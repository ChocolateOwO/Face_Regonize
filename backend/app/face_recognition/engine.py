"""Face detection + embedding wrapper around InsightFace.

One InsightFace FaceAnalysis app is loaded once (loading models is slow) and
reused for every request — warmed at backend startup, not on first request.
Embeddings are L2-normalized, so cosine similarity between two of them is
just a dot product (see face_recognition/index.py for the actual matching).

Only the "detection" and "recognition" sub-models are loaded. InsightFace's
buffalo_l pack ships five models (detection, two landmark models, gender/age,
recognition) and its convenience FaceAnalysis.get() call runs all five per
face by default — but we only ever read the bbox and the embedding, so the
landmark/gender-age inferences were pure waste. Restricting allowed_modules
roughly halves per-face latency for free, no accuracy trade-off, since we
never used their output.
"""
from __future__ import annotations

import gc
import logging
import math
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

import cv2
import numpy as np

# MUST run before insightface (and therefore onnxruntime) is imported: it makes
# the bundled CUDA/cuDNN DLLs loadable, which is what decides whether the GPU
# provider can start at all. Purely a loader fix — see cuda_dlls for why, and
# it is best-effort, so a machine without CUDA still falls through to CPU below.
from app.face_recognition import cuda_dlls

cuda_dlls.enable()

from insightface.app import FaceAnalysis  # noqa: E402 — must follow cuda_dlls.enable()
from insightface.app.common import Face  # noqa: E402

logger = logging.getLogger(__name__)

_face_app: FaceAnalysis | None = None
MODEL_NAME = "buffalo_l"

# Phase G2 — process-wide inference arbiter. Wraps every direct call into
# the ONE shared FaceAnalysis session (det_model.detect()/
# models["recognition"].get()), whether from detect_faces() below (kiosk/
# enrollment) or from event_photo_detection_service.py's tiled scan (Event
# Auto mode) — both acquire this SAME lock object. Concurrent calls into
# one loaded ONNX session are undocumented-behavior territory; this makes
# that actually safe instead of accidentally-usually-fine. An Event batch
# running under an EXPLICITLY selected dedicated device (Phase G3) would
# build its own separate FaceAnalysis instance and never touch this lock —
# nothing else uses that session, so nothing else needs to wait for it.
inference_lock = threading.Lock()

# Two inference lanes (2026-10-20, request from the Mice robot project):
#
#   BACKGROUND  Event Photo, video, import, enrollment and every other
#               non-interactive caller. Uses the session above and
#               inference_lock, exactly as before.
#   LIVE        /api/look, the kiosk and camera nodes. In "dual" mode it has its
#               OWN FaceAnalysis session and its own lock, so a live frame
#               never waits behind a background scan. In "shared" mode (CPU
#               only, not enough GPU memory, second session failed, or
#               RECONIZE_INFERENCE_MODE=shared) both lanes are the same session
#               and inference_lock; background work then steps aside for a
#               waiting live caller before each of its lock acquisitions
#               (services/inference_priority.py).
#
# Both lanes use the one recognition index (face_recognition/index.py) for
# matching; the sessions only produce embeddings, so enrollment/import/delete
# are visible to both immediately. Fair ordering between live callers is the
# job of services/live_scheduler.py, which runs before the live lock.
LIVE = "live"
BACKGROUND = "background"

# (lane, workload) for detect_faces() calls made inside `with use(...)`.
# A context variable rather than new parameters, so every existing caller
# (and test double) of detect_faces(img, timings=None) keeps its signature.
_context: ContextVar[tuple[str, str | None]] = ContextVar("inference_context", default=(BACKGROUND, None))


@contextmanager
def use(lane: str = BACKGROUND, workload: str | None = None):
    """Run the enclosed detect_faces() calls on `lane`, labelled `workload`
    in /api/health ("enrollment", "import", "kiosk", ...)."""
    token = _context.set((lane, workload))
    try:
        yield
    finally:
        _context.reset(token)

_init_lock = threading.RLock()
_live_lock = threading.Lock()
_live_app: FaceAnalysis | None = None
_mode: dict = {"mode": "starting", "reason": "model not loaded yet", "device": "unknown",
               "live_provider": None, "background_provider": None, "live_vram_mb": None}

# Who holds each lock right now, for /api/health. Values are workload labels
# ("event_photo", "look", ...) — never a camera name or a person.
_state_guard = threading.Lock()
_holders: dict[int, tuple[str, float]] = {}
_background_waiting = 0
_call_ms: dict[str, float] = {}  # EMA of one locked call per workload

# Startup-time GPU budget for the second (live) session. The session itself
# measured ~0.5-1 GB on CUDA; the margin keeps room for video sessions and
# NVENC. Both can be overridden without a code change.
LIVE_VRAM_NEED_MB = int(os.environ.get("RECONIZE_LIVE_VRAM_NEED_MB", "1024"))
LIVE_VRAM_MARGIN_MB = int(os.environ.get("RECONIZE_LIVE_VRAM_MARGIN_MB", "1024"))


class InferenceBusy(RuntimeError):
    """detect_faces(lock_timeout=...) could not get the inference lock in
    time. Only callers that pass a timeout (the stateless /api/look) ever see
    this; every existing caller waits for the lock exactly as before.
    retry_after is a best estimate in seconds of when the lock frees up."""

    def __init__(self, message: str = "Face inference is busy.", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def get_face_app() -> FaceAnalysis:
    """The BACKGROUND session (also the only one in shared mode)."""
    global _face_app
    if _face_app is not None:
        return _face_app
    with _init_lock:  # one load even if two threads ask at once
        if _face_app is None:
            _load_background_app()
    return _face_app


def _load_background_app() -> None:
    global _face_app
    if _face_app is None:
        # One process-wide verification decides the provider (see
        # hardware_info_service.verified_providers): TensorRT is skipped when
        # its runtime is absent, and CUDA is only requested once a real session
        # has proved it initialises — so this session uses exactly the provider
        # /hardware-info reports, and the first Event batch re-probes nothing.
        from app.services.hardware_info_service import verified_providers

        try:
            usable = verified_providers()
        except Exception:  # noqa: BLE001 — verification must never block startup
            logger.info("Provider verification unavailable; trying CUDA, then CPU", exc_info=True)
            usable = ["CUDAExecutionProvider", "CPUExecutionProvider"]

        try:
            if "CUDAExecutionProvider" not in usable:
                raise RuntimeError("CUDAExecutionProvider is not usable on this machine")
            app = FaceAnalysis(
                name=MODEL_NAME,
                providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
                allowed_modules=["detection", "recognition"],
            )
            app.prepare(ctx_id=0, det_size=(320, 320))
            active = app.models["recognition"].session.get_providers()
            if "CUDAExecutionProvider" not in active:
                raise RuntimeError(f"CUDA provider not active, got {active}")
            logger.info("Face recognition running on GPU (CUDAExecutionProvider)")
            _face_app = app
        except Exception as e:  # noqa: BLE001 — any GPU init failure should fall back, not crash startup
            logger.warning("GPU init failed (%s), falling back to CPU", e)
            app = FaceAnalysis(
                name=MODEL_NAME,
                providers=["CPUExecutionProvider"],
                allowed_modules=["detection", "recognition"],
            )
            app.prepare(ctx_id=0, det_size=(320, 320))
            _face_app = app

        # Pay ONNX Runtime's first-call cost (memory arena growth, kernel
        # selection) here at startup, not on the user's first real scan.
        t0 = time.perf_counter()
        with use(BACKGROUND, "warmup"):
            detect_faces(np.zeros((320, 320, 3), dtype=np.uint8))
        logger.info("Model warm-up inference took %.0fms", (time.perf_counter() - t0) * 1000)
        provider = _provider_of(_face_app)
        with _state_guard:
            _mode.update(background_provider=provider, device=_device_name(provider))


def _provider_of(app) -> str:
    try:
        providers = app.models["recognition"].session.get_providers()
    except Exception:  # noqa: BLE001 — a stub or a half-built session
        return "unknown"
    return providers[0] if providers else "unknown"


def _device_name(provider: str | None) -> str:
    return {"CUDAExecutionProvider": "cuda", "TensorrtExecutionProvider": "tensorrt",
            "CPUExecutionProvider": "cpu"}.get(provider or "", "unknown")


def _free_vram_mb() -> int | None:
    """Free memory on GPU 0 from nvidia-smi, or None when it cannot be read."""
    try:
        from app.services.hardware_info_service import _gpus_via_nvidia_smi

        gpus = _gpus_via_nvidia_smi()
    except Exception:  # noqa: BLE001
        return None
    gpu0 = next((g for g in gpus if g.index == 0), gpus[0] if gpus else None)
    return int(gpu0.free_vram_mb) if gpu0 else None


def _build_live_app(provider: str):
    """A second FaceAnalysis on the SAME provider as the background session,
    with the same pack and detector size, so its embeddings are the same
    embeddings. Raises if the provider silently fell back."""
    providers = [provider] if provider == "CPUExecutionProvider" else [provider, "CPUExecutionProvider"]
    app = FaceAnalysis(name=MODEL_NAME, providers=providers, allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=-1 if provider == "CPUExecutionProvider" else 0, det_size=(320, 320))
    for model in (app.det_model, app.models["recognition"]):
        active = model.session.get_providers()
        if not active or active[0] != provider:
            raise RuntimeError(f"live session started on {active}, expected {provider}")
    return app


def init_live_lane(requested: str | None = None, *, builder=_build_live_app, free_vram=_free_vram_mb) -> dict:
    """Decide dual or shared mode once, at startup, after get_face_app().

    requested: "auto" (default), "dual" or "shared"; read from
    RECONIZE_INFERENCE_MODE when not given. Never raises: any failure leaves
    shared mode (the pre-2026-10-20 behaviour) with the reason recorded.
    Idempotent: a second call returns the existing decision.
    """
    global _live_app
    with _init_lock:
        if _mode["mode"] in ("dual", "shared"):
            return mode_info()
        requested = (requested or os.environ.get("RECONIZE_INFERENCE_MODE", "auto")).strip().lower()
        background = get_face_app()
        provider = _provider_of(background)

        def shared(reason: str) -> dict:
            with _state_guard:
                _mode.update(mode="shared", reason=reason, live_provider=provider,
                             background_provider=provider, device=_device_name(provider))
            logger.info("Inference mode: shared (%s)", reason)
            return mode_info()

        if requested == "shared":
            return shared("RECONIZE_INFERENCE_MODE=shared")
        if requested not in ("auto", "dual"):
            return shared(f"unknown RECONIZE_INFERENCE_MODE={requested!r}; using shared")
        if provider == "CPUExecutionProvider" and requested != "dual":
            return shared("CPU only: a second CPU session would compete for the same cores")
        if provider not in ("CUDAExecutionProvider", "CPUExecutionProvider"):
            return shared(f"background provider is {provider}")

        before = free_vram() if provider == "CUDAExecutionProvider" else None
        if before is not None and before < LIVE_VRAM_NEED_MB + LIVE_VRAM_MARGIN_MB:
            return shared(f"not enough free GPU memory for a second session: {before} MB free, "
                          f"{LIVE_VRAM_NEED_MB + LIVE_VRAM_MARGIN_MB} MB needed")
        app = None
        try:
            app = builder(provider)
            with _live_lock:  # first-call cost now, not on the first robot frame
                app.det_model.detect(np.zeros((320, 320, 3), np.uint8), max_num=0, metric="default")
                app.models["recognition"].get_feat(np.zeros((112, 112, 3), np.uint8))
            after = free_vram() if provider == "CUDAExecutionProvider" else None
            if after is not None and after < LIVE_VRAM_MARGIN_MB:
                raise RuntimeError(f"only {after} MB GPU memory left after loading the live session")
        except Exception as exc:  # noqa: BLE001 — fall back, never crash startup
            app = None
            gc.collect()  # drop the half-built ONNX sessions now
            logger.warning("Second (live) inference session not used: %s", exc)
            return shared(f"second session failed: {exc}")
        _live_app = app
        used = (before - after) if before is not None and after is not None else None
        with _state_guard:
            _mode.update(mode="dual", reason=f"separate live session on {provider}", live_provider=provider,
                         background_provider=provider, device=_device_name(provider),
                         live_vram_mb=max(used, 0) if used is not None else None)
        logger.info("Inference mode: dual (live session on %s, ~%s MB)", provider, used)
        return mode_info()


def release_live_lane() -> None:
    """Drop the live session (tests, shutdown). Live work then shares the
    background session again."""
    global _live_app
    with _init_lock:
        with _live_lock:
            _live_app = None
        gc.collect()
        with _state_guard:
            _mode.update(mode="starting", reason="live lane released", live_provider=None, live_vram_mb=None)


# Hybrid face embedding (OPT-IN: RECONIZE_HYBRID_EMBED=1; off by default).
#
# On onnxruntime 1.29 + CUDA every change of the recognition model's batch
# size costs extra time (measured here 2026-11-25: 120-270 ms per switch on an
# RTX 4050 Laptop), which is why embedding all faces of a frame in one
# variable-size batch (F1) was rejected. The hybrid keeps every session at ONE
# shape: the live lane's existing recognition session only ever sees batch 1
# (frames with 1-2 faces, embedded one by one exactly as before), and a
# SECOND recognition session on the same model file only ever sees batch 8
# (frames with 3+ faces: aligned exactly as ArcFaceONNX.get() does, padded
# with copies of the last face to 8, chunks of 8, padded rows discarded).
# Only the LIVE lane uses it (/api/look, /api/look/batch, kiosk, stations);
# background work keeps the per-face loop. CUDA only, built once at startup
# when enough GPU memory is free, warmed at batch 1 and 8; any failure keeps
# the per-face path. Results and the decision to keep it opt-in:
# docs/experiments/2026-11-26-hybrid-embedding.md.
HYBRID_BATCH = 8
HYBRID_MIN_FACES = 3
HYBRID_VRAM_NEED_MB = int(os.environ.get("RECONIZE_HYBRID_VRAM_NEED_MB", "512"))
_hybrid: dict = {"enabled": False, "reason": "not initialised", "app": None, "session": None,
                 "startup_ms": None, "vram_mb": None}


def _build_rec8(app):
    """A second ArcFace session on the same model file and provider as `app`'s."""
    import onnxruntime
    from insightface.model_zoo.arcface_onnx import ArcFaceONNX

    rec = app.models["recognition"]
    provider = rec.session.get_providers()[0]
    session = onnxruntime.InferenceSession(rec.model_file, providers=[provider, "CPUExecutionProvider"])
    if session.get_providers()[0] != provider:
        raise RuntimeError(f"batch-8 session started on {session.get_providers()}, expected {provider}")
    return ArcFaceONNX(model_file=rec.model_file, session=session)


def init_hybrid(requested: str | None = None, *, builder=_build_rec8, free_vram=_free_vram_mb) -> dict:
    """Decide once, after init_live_lane(). Never raises."""
    with _init_lock:
        if _hybrid["reason"] != "not initialised":
            return hybrid_info()
        flag = (requested if requested is not None else os.environ.get("RECONIZE_HYBRID_EMBED", "0")).strip().lower()

        def off(reason: str) -> dict:
            _hybrid.update(enabled=False, reason=reason, app=None, session=None)
            logger.info("Hybrid embedding off: %s", reason)
            return hybrid_info()

        if flag not in ("1", "true", "yes", "on"):
            return off("RECONIZE_HYBRID_EMBED is not set (default: per-face embedding)")
        app = _app_for(LIVE)
        if _provider_of(app) != "CUDAExecutionProvider":
            return off(f"live provider is {_provider_of(app)}, not CUDA")
        before = free_vram()
        if before is not None and before < HYBRID_VRAM_NEED_MB + LIVE_VRAM_MARGIN_MB:
            return off(f"not enough free GPU memory: {before} MB free, {HYBRID_VRAM_NEED_MB + LIVE_VRAM_MARGIN_MB} MB needed")
        t0 = time.perf_counter()
        try:
            rec8 = builder(app)
            with lock_for(LIVE):  # warm-up: batch 8 here, batch 1 was warmed by init_live_lane
                rec8.get_feat([np.zeros((112, 112, 3), np.uint8)] * HYBRID_BATCH)
                app.models["recognition"].get_feat(np.zeros((112, 112, 3), np.uint8))
        except Exception as exc:  # noqa: BLE001 - keep the per-face path
            gc.collect()
            return off(f"batch-8 session failed: {exc}")
        after = free_vram()
        _hybrid.update(enabled=True, reason=f"batch-8 session for {HYBRID_MIN_FACES}+ faces on the live lane",
                       app=app, session=rec8, startup_ms=round((time.perf_counter() - t0) * 1000, 1),
                       vram_mb=max(before - after, 0) if before is not None and after is not None else None)
        logger.info("Hybrid embedding on (%s ms, ~%s MB)", _hybrid["startup_ms"], _hybrid["vram_mb"])
        return hybrid_info()


def release_hybrid() -> None:
    """Tests and shutdown."""
    with _init_lock:
        _hybrid.update(enabled=False, reason="not initialised", app=None, session=None, startup_ms=None, vram_mb=None)
        gc.collect()


def hybrid_info() -> dict:
    return {k: _hybrid[k] for k in ("enabled", "reason", "startup_ms", "vram_mb")}


def _hybrid_session(app, lane: str):
    if lane == LIVE and _hybrid["enabled"] and _hybrid["app"] is app:
        return _hybrid["session"]
    return None


def _embed_aligned(rec, rec8, aligned: list) -> np.ndarray:
    """Raw (unnormalised) embeddings for aligned 112x112 faces. With rec8:
    every call is exactly HYBRID_BATCH rows; otherwise one face per call on
    the single-face session. Never a third batch size."""
    if not aligned:
        return np.zeros((0, 512), np.float32)
    if rec8 is None or len(aligned) < HYBRID_MIN_FACES:
        return np.stack([np.asarray(rec.get_feat(a)).reshape(-1) for a in aligned])
    out = []
    for i in range(0, len(aligned), HYBRID_BATCH):
        chunk = aligned[i:i + HYBRID_BATCH]
        padded = chunk + [chunk[-1]] * (HYBRID_BATCH - len(chunk))
        out.append(np.asarray(rec8.get_feat(padded))[:len(chunk)])
    return np.concatenate(out).reshape(len(aligned), -1)


def is_dual() -> bool:
    return _live_app is not None


def mode_info() -> dict:
    with _state_guard:
        return dict(_mode)


def is_ready() -> bool:
    """True once the shared model is loaded and warmed (startup does this
    before serving). Read-only: never triggers the slow load itself."""
    return _face_app is not None


def get_active_provider() -> str:
    return _provider_of(get_face_app())


def _app_for(lane: str):
    if lane == LIVE and _live_app is not None:
        return _live_app
    return get_face_app()


def lock_for(lane: str) -> threading.Lock:
    """The lock a call on this lane needs. In shared mode both lanes need
    inference_lock."""
    return _live_lock if lane == LIVE and _live_app is not None else inference_lock


@contextmanager
def hold(lane: str = BACKGROUND, workload: str = "background", timeout: float | None = None,
         yield_first: bool = True):
    """Hold one lane's lock for ONE inference call.

    Background callers step aside first for a waiting live caller, but only
    when the live caller needs the same lock (shared mode). timeout=None
    waits as before; a number raises InferenceBusy when it runs out.

    With MICE_HUB_GPU_LEASE=1 a BACKGROUND call first asks the Mice Hub for
    the shared GPU (services/hub_scheduler.py) - before, never while, holding
    the local lock, and fail-open. Live calls never ask."""
    if lane == BACKGROUND and workload != "warmup":
        from app.services import hub_scheduler

        with hub_scheduler.lease(workload):
            with _hold_local(lane, workload, timeout, yield_first):
                yield
    else:
        with _hold_local(lane, workload, timeout, yield_first):
            yield


@contextmanager
def _hold_local(lane: str, workload: str, timeout: float | None, yield_first: bool):
    lock = lock_for(lane)
    if lane == BACKGROUND and yield_first and lock_for(LIVE) is lock:  # shared mode only
        from app.services import inference_priority

        inference_priority.yield_to_interactive()
    global _background_waiting
    if lane == BACKGROUND:
        with _state_guard:
            _background_waiting += 1
    try:
        if timeout is None:
            got = lock.acquire()
        else:
            got = lock.acquire(timeout=max(timeout, 0.0))
    finally:
        if lane == BACKGROUND:
            with _state_guard:
                _background_waiting -= 1
    if not got:
        raise InferenceBusy("Face inference is busy.", retry_after=estimated_free_in(lock))
    began = time.perf_counter()
    with _state_guard:
        _holders[id(lock)] = (workload, began)
    try:
        yield
    finally:
        took = (time.perf_counter() - began) * 1000
        with _state_guard:
            _holders.pop(id(lock), None)
            prev = _call_ms.get(workload)
            _call_ms[workload] = took if prev is None else prev * 0.8 + took * 0.2
        lock.release()


def estimated_free_in(lock: threading.Lock | None = None) -> float:
    """Seconds until the current holder of `lock` is expected to finish its
    call, from that workload's recent call times. 0.0 when it is free."""
    lock = lock or inference_lock
    with _state_guard:
        holder = _holders.get(id(lock))
        if holder is None:
            return 0.0
        workload, began = holder
        typical = _call_ms.get(workload, 100.0) / 1000
    return max(typical - (time.perf_counter() - began), 0.0) + 0.05


def busy_with(lane: str) -> str | None:
    with _state_guard:
        holder = _holders.get(id(lock_for(lane)))
    return holder[0] if holder else None


def status() -> dict:
    """Cheap, lock-free-for-inference snapshot for /api/health."""
    info = mode_info()
    with _state_guard:
        bg = _holders.get(id(inference_lock))
        live = _holders.get(id(_live_lock)) if _live_app is not None else bg
        info.update(
            background_busy_with=bg[0] if bg else None,
            live_busy_with=live[0] if live else None,
            background_waiting=_background_waiting,
            typical_call_ms={k: round(v, 1) for k, v in _call_ms.items() if math.isfinite(v)},
        )
    info["hybrid_embed"] = hybrid_info()
    return info


class DetectedFace:
    def __init__(self, embedding: np.ndarray, bbox: tuple[float, float, float, float], det_score: float):
        self.embedding = embedding
        self.bbox = bbox
        self.det_score = det_score


def resize_for_detection(image_bgr: np.ndarray, max_side: int = 960) -> tuple[np.ndarray, float]:
    """Downscale large images before detection — recognition quality is
    dominated by the detector's own internal resolution (det_size), so
    feeding it a multi-megapixel original just costs decode/resize time for
    no benefit. Returns (working_image, scale) where scale maps working-image
    coordinates back to the original (bbox * (1/scale))."""
    h, w = image_bgr.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return image_bgr, 1.0
    scale = max_side / longest
    resized = cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return resized, scale


def detect_faces(image_bgr: np.ndarray, timings: dict | None = None,
                 lock_timeout: float | None = None, *, lane: str | None = None,
                 workload: str | None = None) -> list[DetectedFace]:
    """Re-implements FaceAnalysis.get() by hand (rather than calling it
    directly) so detection and embedding can be timed as separate stages —
    the alignment/crop step is a cheap numpy warp done inline inside the
    recognition model's .get(), so it's folded into the "embedding" number
    rather than reported as its own stage.

    lock_timeout=None (every existing caller) waits for the lock as before; a
    number makes the wait bounded and raises InferenceBusy instead.
    The lane comes from `with use(...)` (live callers get it from
    services/live_scheduler.live_turn()); without one it is BACKGROUND, so
    unchanged callers keep their old session and lock."""
    ctx_lane, ctx_workload = _context.get()
    lane = lane or ctx_lane
    workload = workload or ctx_workload
    app = _app_for(lane)

    with hold(lane, workload or lane, timeout=lock_timeout):
        t0 = time.perf_counter()
        bboxes, kpss = app.det_model.detect(image_bgr, max_num=0, metric="default")
        t_detect = time.perf_counter()

        faces: list[Face] = []
        for i in range(bboxes.shape[0]):
            bbox = bboxes[i, 0:4]
            det_score = bboxes[i, 4]
            kps = kpss[i] if kpss is not None else None
            faces.append(Face(bbox=bbox, kps=kps, det_score=det_score))

        rec_model = app.models["recognition"]
        rec8 = _hybrid_session(app, lane)
        if rec8 is None or len(faces) < HYBRID_MIN_FACES:
            for face in faces:  # unchanged per-face path (batch 1)
                rec_model.get(image_bgr, face)
        else:
            from insightface.utils import face_align

            aligned = [face_align.norm_crop(image_bgr, landmark=f.kps, image_size=rec_model.input_size[0]) for f in faces]
            for face, feat in zip(faces, _embed_aligned(rec_model, rec8, aligned)):
                face.embedding = feat.flatten()
        t_embed = time.perf_counter()

    if timings is not None:
        timings["detection_ms"] = (t_detect - t0) * 1000
        timings["embedding_ms"] = (t_embed - t_detect) * 1000

    return [
        DetectedFace(
            embedding=face.normed_embedding.astype(np.float32),
            bbox=tuple(face.bbox.tolist()),
            det_score=float(face.det_score),
        )
        for face in faces
    ]


def detect_faces_batch(images: list[np.ndarray], timings: dict | None = None,
                       lock_timeout: float | None = None) -> list[list[DetectedFace]]:
    """detect_faces() for several frames under ONE lock hold (robot hub F6).

    The installed SCRFD detector has a fixed batch size of 1, so frames are
    detected one after another; every face from every frame is then aligned
    exactly as ArcFaceONNX.get() does and embedded in ONE batched
    recognition call (that model accepts any batch size)."""
    from insightface.utils import face_align

    ctx_lane, ctx_workload = _context.get()
    app = _app_for(ctx_lane)
    per_image: list[list[Face]] = []
    with hold(ctx_lane, ctx_workload or ctx_lane, timeout=lock_timeout):
        t0 = time.perf_counter()
        for image in images:
            bboxes, kpss = app.det_model.detect(image, max_num=0, metric="default")
            per_image.append([Face(bbox=bboxes[i, 0:4], kps=kpss[i] if kpss is not None else None,
                                   det_score=bboxes[i, 4]) for i in range(bboxes.shape[0])])
        t_detect = time.perf_counter()
        rec_model = app.models["recognition"]
        aligned = [face_align.norm_crop(image, landmark=face.kps, image_size=rec_model.input_size[0])
                   for image, faces in zip(images, per_image) for face in faces]
        rec8 = _hybrid_session(app, ctx_lane)
        if rec8 is not None:  # hybrid: batch 1 or batch 8 only, never a variable size
            feats = _embed_aligned(rec_model, rec8, aligned)
        else:
            feats = rec_model.get_feat(aligned) if aligned else np.zeros((0, 512), np.float32)
        t_embed = time.perf_counter()

    if timings is not None:
        timings["detection_ms"] = (t_detect - t0) * 1000
        timings["embedding_ms"] = (t_embed - t_detect) * 1000
    out, k = [], 0
    for faces in per_image:
        found = []
        for face in faces:
            emb = np.asarray(feats[k], dtype=np.float32).flatten()
            k += 1
            norm = float(np.linalg.norm(emb)) or 1.0
            found.append(DetectedFace(embedding=(emb / norm).astype(np.float32),
                                      bbox=tuple(face.bbox.tolist()), det_score=float(face.det_score)))
        out.append(found)
    return out


def bbox_to_str(bbox: tuple[float, float, float, float]) -> str:
    return ",".join(f"{v:.1f}" for v in bbox)
