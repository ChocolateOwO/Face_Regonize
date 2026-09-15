"""Phase F1 — admin-triggered historical backfill of evaluation candidates.

Batches processed before capture existed have no candidates. This re-derives
embeddings for their already-finalized photos and writes candidates through
exactly the same bounded, idempotent path live capture uses
(identity_candidate_service.replace_candidates).

Isolation from live recognition, by construction:
  * its OWN FaceAnalysis instance — never the kiosk singleton from
    engine.get_face_app(), and never engine.inference_lock;
  * no matching at all: `predicted_person_id` is copied from the face row the
    matcher wrote when the photo was processed (no match_batch call);
  * ORIGINAL is read server-side only to compute embeddings — MEDIA has
    declined faces masked, so it would produce meaningless vectors. Nothing
    read here is returned by any API.

Safety rails before and during a run:
  * CUDA must be a usable provider and the GPU must have free VRAM for the
    instance plus a margin; the instance's real VRAM cost is measured at
    warm-up and the run aborts if that left too little headroom;
  * one job at a time, bounded by max_photos, cancellable between photos;
  * it pauses while an Event batch is processing, while the shared inference
    lock is held, or within KIOSK_QUIET_S of a kiosk recognition — a separate
    instance still shares the one physical GPU.
"""
from __future__ import annotations

import gc
from datetime import datetime, timedelta
import logging
import threading
import time

import cv2
import numpy as np
from sqlmodel import Session, func, select

logger = logging.getLogger(__name__)

MAX_PHOTOS_DEFAULT = 200
MAX_PHOTOS_CAP = 2000
VRAM_NEED_MB = 1200
VRAM_MARGIN_MB = 512
KIOSK_QUIET_S = 30
PAUSE_POLL_S = 2.0
IDLE_BATCH_STATUSES = ("ready", "completed", "upload_failed", "needs_retry", "failed")


class BackfillRefused(RuntimeError):
    """A start request that must not run. The message is shown to the admin."""


_job_lock = threading.Lock()
_cancel = threading.Event()
_status: dict = {"state": "idle"}


def status() -> dict:
    with _job_lock:
        return dict(_status)


def _set(**fields) -> None:
    with _job_lock:
        _status.update(fields)


def cancel() -> dict:
    _cancel.set()
    with _job_lock:
        if _status.get("state") in ("running", "paused"):
            _status["state"] = "cancelling"
        return dict(_status)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _hardware():
    from app.services.hardware_info_service import get_hardware_info

    return get_hardware_info()


def _free_vram_mb(hw) -> int:
    return max((int(g.free_vram_mb) for g in getattr(hw, "gpus", []) or []), default=0)


def preflight(hw_probe=_hardware) -> dict:
    hw = hw_probe()
    if "CUDAExecutionProvider" not in (getattr(hw, "usable_providers", []) or []):
        raise BackfillRefused("The backfill needs a usable CUDA GPU; none is available on this machine.")
    free = _free_vram_mb(hw)
    if free < VRAM_NEED_MB + VRAM_MARGIN_MB:
        raise BackfillRefused(
            f"Not enough free GPU memory: {free} MB free, {VRAM_NEED_MB + VRAM_MARGIN_MB} MB needed "
            "(the model plus a safety margin for live recognition)."
        )
    return {"free_vram_mb": free}


def _build_face_app():
    """A dedicated instance. Same pack and detector size as the kiosk, so the
    embeddings are comparable — but its own ONNX sessions."""
    from insightface.app import FaceAnalysis

    from app.face_recognition.engine import MODEL_NAME

    app = FaceAnalysis(name=MODEL_NAME, providers=["CUDAExecutionProvider"],
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(320, 320))
    if "CUDAExecutionProvider" not in app.models["recognition"].session.get_providers():
        raise BackfillRefused("The backfill model did not start on the GPU.")
    return app


# ---------------------------------------------------------------------------
# Contention
# ---------------------------------------------------------------------------

def busy_reason(engine) -> str | None:
    """Why the backfill should wait right now, or None."""
    from app.face_recognition import engine as face_engine
    from app.models.models import Attendance, FaceDetection
    from app.services import photo_processing_service as pps
    from app.services.event_pipeline_registry import registry as pipeline_registry

    with pps._batch_lock:
        if pps._active_batches:
            return "an Event batch is processing"
    if pipeline_registry.any_active():
        return "an Event batch is processing"
    if face_engine.inference_lock.locked():
        return "live recognition is running"
    since = datetime.now() - timedelta(seconds=KIOSK_QUIET_S)
    with Session(engine) as session:
        for column in (FaceDetection.detected_at, Attendance.detected_at):
            latest = session.exec(select(func.max(column))).one()
            if latest is not None and latest >= since:
                return "a kiosk recognition happened recently"
    return None


# ---------------------------------------------------------------------------
# Work selection
# ---------------------------------------------------------------------------

def pending_photos(engine, version: str, *, batch_id: str | None, limit: int) -> list[tuple[str, str]]:
    """(batch_id, photo_id) of finalized photos with faces and no candidates
    for this extractor version yet, in a deterministic order."""
    from app.models.identity_models import EventIdentityCandidate
    from app.models.models import PhotoBatch, PhotoBatchPhoto

    captured = select(EventIdentityCandidate.photo_id).where(EventIdentityCandidate.extractor_version == version)
    query = (
        select(PhotoBatchPhoto.batch_id, PhotoBatchPhoto.id)
        .join(PhotoBatch, PhotoBatch.id == PhotoBatchPhoto.batch_id)
        .where(PhotoBatch.status.in_(IDLE_BATCH_STATUSES))
        .where(PhotoBatchPhoto.media_path.is_not(None))
        .where(PhotoBatchPhoto.faces_total > 0)
        .where(PhotoBatchPhoto.id.not_in(captured))
        .order_by(PhotoBatch.created_at, PhotoBatchPhoto.filename)
        .limit(limit)
    )
    if batch_id:
        query = query.where(PhotoBatchPhoto.batch_id == batch_id)
    with Session(engine) as session:
        return [(b, p) for b, p in session.exec(query).all()]


def _original_path(storage_path, photo_batches_dir, batch, photo):
    candidates = []
    if photo.original_path:
        candidates.append(storage_path / photo.original_path)
    candidates.append(photo_batches_dir / batch.id / "ORIGINAL" / photo.filename)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _embed(app, img: np.ndarray) -> list:
    """Detect with the Event tiling and embed, on the DEDICATED instance."""
    from insightface.app.common import Face

    from app.face_recognition.engine import DetectedFace
    from app.services.event_photo_detection_service import collect_candidates

    faces = []
    for candidate in collect_candidates(img, app.det_model):
        face = Face(bbox=candidate.bbox.astype(np.float32), kps=candidate.kps, det_score=candidate.score)
        app.models["recognition"].get(img, face)
        faces.append(DetectedFace(face.normed_embedding.astype(np.float32),
                                  tuple(candidate.bbox.tolist()), candidate.score))
    return faces


def _backfill_photo(app, engine, storage_path, photo_batches_dir, batch_id: str, photo_id: str,
                    threshold: float) -> int:
    from app.models.models import PhotoBatch, PhotoBatchFace, PhotoBatchPhoto
    from app.services import identity_candidate_service as ics

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        photo = session.get(PhotoBatchPhoto, photo_id)
        if batch is None or photo is None:
            return 0
        path = _original_path(storage_path, photo_batches_dir, batch, photo)
        if path is None:
            return 0
        img = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return 0
        faces = _embed(app, img)
        stored = {f.bbox: f for f in session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo_id)).all()}
        linked = [stored.get(",".join(f"{v:.1f}" for v in face.bbox)) for face in faces]
        rows = ics.build_candidates(engine=engine, batch_id=batch_id, filename=photo.filename, img=img, faces=faces,
                                    predicted=[row.person_id if row else None for row in linked], threshold=threshold)
        ics.replace_candidates(session, batch_id, photo_id, rows, [row.id if row else None for row in linked])
        session.commit()
        return len(rows)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

def start(*, batch_id: str | None = None, max_photos: int = MAX_PHOTOS_DEFAULT, engine=None, storage_path=None,
          photo_batches_dir=None, builder=_build_face_app, hw_probe=_hardware, busy=busy_reason,
          run_inline: bool = False) -> dict:
    from app.config import PHOTO_BATCHES_DIR, STORAGE_PATH
    from app.database.db import engine as default_engine

    engine = engine if engine is not None else default_engine
    storage_path = storage_path if storage_path is not None else STORAGE_PATH
    photo_batches_dir = photo_batches_dir if photo_batches_dir is not None else PHOTO_BATCHES_DIR
    if not 1 <= int(max_photos) <= MAX_PHOTOS_CAP:
        raise BackfillRefused(f"max_photos must be between 1 and {MAX_PHOTOS_CAP}.")
    with _job_lock:
        if _status.get("state") in ("starting", "running", "paused", "cancelling"):
            raise BackfillRefused("A backfill is already running.")
        _status.clear()
        _status.update(state="starting", batch_id=batch_id, max_photos=int(max_photos), processed=0,
                       captured=0, total=0, pause_reason=None, error=None, vram_delta_mb=None,
                       started_at=datetime.now().isoformat(timespec="seconds"), finished_at=None)
    _cancel.clear()
    try:
        info = preflight(hw_probe)
    except BackfillRefused as e:
        _set(state="refused", error=str(e), finished_at=datetime.now().isoformat(timespec="seconds"))
        raise
    args = (batch_id, int(max_photos), engine, storage_path, photo_batches_dir, builder, hw_probe, busy, info)
    if run_inline:
        _run(*args)
    else:
        threading.Thread(target=_run, args=args, name="identity-backfill", daemon=True).start()
    return status()


def _run(batch_id, max_photos, engine, storage_path, photo_batches_dir, builder, hw_probe, busy, info) -> None:
    from app.services import identity_candidate_service as ics
    from app.services import settings_cache

    app = None
    try:
        before = info["free_vram_mb"]
        app = builder()
        after = _free_vram_mb(hw_probe())
        delta = max(0, before - after)
        _set(vram_delta_mb=delta)
        if after < VRAM_MARGIN_MB:
            raise BackfillRefused(f"Loading the model left only {after} MB of GPU memory; stopped to protect live recognition.")

        version = ics.extractor_version()
        work = pending_photos(engine, version, batch_id=batch_id, limit=max_photos)
        _set(state="running", total=len(work))
        threshold = float(settings_cache.get_threshold())
        for index, (b_id, p_id) in enumerate(work):
            while not _cancel.is_set():
                reason = busy(engine)
                if reason is None:
                    break
                _set(state="paused", pause_reason=reason)
                time.sleep(PAUSE_POLL_S)
            if _cancel.is_set():
                _set(state="cancelled")
                return
            _set(state="running", pause_reason=None)
            try:
                captured = _backfill_photo(app, engine, storage_path, photo_batches_dir, b_id, p_id, threshold)
            except Exception:  # noqa: BLE001 — one photo must not stop the run
                logger.warning("Backfill skipped photo %s", p_id, exc_info=True)
                captured = 0
            with _job_lock:
                _status["processed"] = index + 1
                _status["captured"] += captured
        _set(state="done")
    except BackfillRefused as e:
        _set(state="refused", error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Identity backfill failed")
        _set(state="failed", error=str(e))
    finally:
        app = None
        gc.collect()
        _set(finished_at=datetime.now().isoformat(timespec="seconds"))


def summary(engine=None) -> dict:
    """Counts only — never an embedding, a crop or a path."""
    from app.database.db import engine as default_engine
    from app.models.identity_models import EventIdentityCandidate, EventIdentitySample
    from app.services import identity_candidate_service as ics

    engine = engine if engine is not None else default_engine
    if not ics._has_tables(engine, ics.CANDIDATE_TABLE, ics.SAMPLE_TABLE):
        return {"candidates": 0, "by_bucket": {}, "trusted_samples": 0, "promotion_enabled": False}
    with Session(engine) as session:
        by_bucket = dict(session.exec(select(EventIdentityCandidate.capture_bucket, func.count())
                                      .group_by(EventIdentityCandidate.capture_bucket)).all())
        samples = session.exec(select(func.count()).select_from(EventIdentitySample)).one()
    return {"candidates": sum(by_bucket.values()), "by_bucket": by_bucket, "trusted_samples": samples,
            "promotion_enabled": ics.promotion_enabled()}
