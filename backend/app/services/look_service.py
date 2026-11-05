"""Stateless "who is in this JPEG" for the robot hub (POST /api/look).

The hub on the same PC pulls the latest frame from its own ESP32-CAM MJPEG
stream and asks Central what it sees, at most a few times a second per
camera with one request in flight. Central only evaluates the frame:

    decode -> the SAME detector/session/lock and recognition index/threshold
    the kiosk uses -> answer

and keeps nothing. No image or thumbnail, no Upload/FaceDetection/Attendance
row, no live-feed event, no node presence, no per-source history. That is the
whole contract, so the module imports no database or storage writer; its
only database access is reading Person.call_as/title/language for the
matched ids.

Resource bounds, all enforced before the expensive work:
    bytes      photo <= MAX_PHOTO_BYTES (the route's ASGI guard also caps the
               raw request body while it is still being received)
    format     JPEG only (SOI marker, PIL header says JPEG, EOI present)
    pixels     header dimensions checked before the full decode
    capacity   at most MAX_IN_FLIGHT looks decode at once; each look then waits
               for its source's fair turn on the LIVE lane
               (services/live_scheduler.py) for at most LOCK_TIMEOUT_S, and a
               source may have at most one frame waiting. Any
               limit -> 429 "busy" with an estimated Retry-After, so the hub
               drops the frame instead of queueing a stale one.
"""
from __future__ import annotations

import io
import logging
import math
import re
import threading
import time

import numpy as np
from PIL import Image, UnidentifiedImageError

from sqlmodel import Session

from app.database.db import engine as db_engine
from app.face_recognition import engine
from app.face_recognition.index import recognition_index
from app.services import live_scheduler, settings_cache, storage_service
from app.services.person_address import address_for

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = MAX_PHOTO_BYTES + 64 * 1024  # multipart framing + small form fields
MAX_SIDE = 2048
MAX_PIXELS = 4_200_000
# Decoded frames held at once (memory bound: <= 4.2 MP x 3 bytes each). Raised
# from 4 so several cameras (robot, photobooth, ...) can each have a frame in
# flight; fairness between them is the live scheduler's job.
MAX_IN_FLIGHT = 12
LOCK_TIMEOUT_S = 0.5  # longest a frame waits for its turn + the model
RETRY_AFTER_MAX_S = 10

_admission = threading.BoundedSemaphore(MAX_IN_FLIGHT)
_SOURCE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LookError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None,
                 limit: str | None = None, reason: str | None = None, retry_after_ms: int | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers
        # 429 only: which limit was hit (see BUSY_LIMITS) and the estimate.
        self.limit = limit
        self.reason = reason
        self.retry_after_ms = retry_after_ms


# 429 body "limit": per_source (this camera already has a frame waiting),
# global_queue (too many frames waiting/decoding across all sources), timeout
# (no turn or no model within LOCK_TIMEOUT_S). "reason" is the finer cause.
BUSY_LIMITS = {"source_busy": "per_source", "queue_full": "global_queue", "inflight_full": "global_queue",
               "turn_timeout": "timeout", "model_busy": "timeout"}
_LIVE_REASONS = {"source_busy": "source_busy", "queue_full": "queue_full", "timeout": "turn_timeout"}


def retry_headers(seconds: float | None) -> dict:
    """Retry-After is whole seconds (HTTP delta-seconds), so a sub-second
    estimate rounds up to 1; X-Retry-After-Ms carries the estimate itself."""
    est = max(float(seconds or 0.0), 0.0)
    whole = min(max(math.ceil(est), 1), RETRY_AFTER_MAX_S)
    return {"Retry-After": str(whole), "X-Retry-After-Ms": str(int(min(est, RETRY_AFTER_MAX_S) * 1000))}


def _busy(detail: str, retry_after: float | None = None, reason: str = "queue_full") -> LookError:
    live_scheduler.look_stats.busy()
    if retry_after is None:
        retry_after = live_scheduler.scheduler.estimate_wait()
    headers = retry_headers(retry_after)
    return LookError(429, detail, headers, limit=BUSY_LIMITS[reason], reason=reason,
                     retry_after_ms=int(headers["X-Retry-After-Ms"]))


_inflight_guard = threading.Lock()
_inflight = 0


def inflight() -> dict:
    """Frames currently admitted (decoding or inferring), for /api/health."""
    return {"inflight": _inflight, "inflight_limit": MAX_IN_FLIGHT}


def _admit() -> bool:
    global _inflight
    if not _admission.acquire(blocking=False):
        return False
    with _inflight_guard:
        _inflight += 1
    return True


def _release() -> None:
    global _inflight
    with _inflight_guard:
        _inflight -= 1
    _admission.release()


def clean_source(source: str | None) -> str:
    """The camera label as used for fairness and per-source thresholds."""
    return _SOURCE_RE.sub("_", source or "")[:64]


def source_key(source: str | None, user_id: str | None) -> str:
    """Fair-share key: the caller's camera label, else its account."""
    cleaned = clean_source(source)
    return f"look:{cleaned}" if cleaned else f"look:account:{user_id or 'unknown'}"


def _check_jpeg(data: bytes) -> tuple[int, int]:
    if len(data) > MAX_PHOTO_BYTES:
        raise LookError(413, f"Photo exceeds {MAX_PHOTO_BYTES // (1024 * 1024)} MiB.")
    if not data.startswith(b"\xff\xd8\xff"):
        raise LookError(400, "Photo is not a JPEG.")
    # A camera frame cut off mid-transfer still often "decodes" with grey
    # rows; the end-of-image marker is the cheap honest check for that.
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise LookError(400, "JPEG is truncated (no end-of-image marker).")
    try:
        with Image.open(io.BytesIO(data)) as header:  # reads the header only
            fmt, (w, h) = header.format, header.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise LookError(400, "Could not read the JPEG header.") from exc
    if fmt != "JPEG":
        raise LookError(400, "Photo is not a JPEG.")
    if w <= 0 or h <= 0:
        raise LookError(400, "JPEG has no dimensions.")
    if max(w, h) > MAX_SIDE or w * h > MAX_PIXELS:
        raise LookError(413, f"JPEG dimensions {w}x{h} exceed {MAX_SIDE}px per side / {MAX_PIXELS} pixels.")
    return w, h


def _box(bbox, scale: float, w: int, h: int) -> list[float]:
    x1, y1, x2, y2 = (v / scale for v in bbox)
    clip = lambda v, limit: min(max(v / limit, 0.0), 1.0)  # noqa: E731
    return [round(clip(x1, w), 4), round(clip(y1, h), 4), round(clip(x2, w), 4), round(clip(y2, h), 4)]


def _faces_out(faces, matches, addresses: dict, scale: float, w: int, h: int) -> list[dict]:
    out = []
    for face, (person_id, first_name, full_name, participant_id, score) in zip(faces, matches):
        box = _box(face.bbox, scale, w, h)
        entry = {"box": box, "face_score": round(float(face.det_score), 4),
                 "status": "matched" if person_id else "unknown", "tags": []}
        if person_id:
            address = addresses.get(person_id, {})
            entry.update(person_id=person_id, participant_id=participant_id, first_name=first_name,
                         name=full_name, call_as=address.get("call_as"), title=address.get("title"),
                         language=address.get("language"), tags=address.get("tags", []),
                         confidence=round(float(score), 4))
        out.append((max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0), entry))
    # Largest (usually nearest) face first; stable for equal areas.
    out.sort(key=lambda pair: -pair[0])
    return [entry for _area, entry in out]


def _addresses(matches_lists) -> dict:
    matched_ids = [m[0] for matches in matches_lists for m in matches if m[0]]
    if not matched_ids:
        return {}
    with Session(db_engine) as session:
        return address_for(session, matched_ids)


def look(data: bytes, source: str | None = None, user_id: str | None = None) -> dict:
    started = time.perf_counter()
    w, h = _check_jpeg(data)
    if not engine.is_ready():
        raise LookError(503, "Recognition model is not loaded yet.")
    if not _admit():
        raise _busy("Too many look requests in progress; drop this frame.", reason="inflight_full")
    try:
        img = storage_service.decode_image(data)
        if img is None:
            raise LookError(400, "Could not decode the JPEG.")
        h, w = img.shape[:2]
        working, scale = engine.resize_for_detection(img)
        timings: dict = {}
        try:
            # Fair turn among live sources, then the LIVE lane (its own model
            # in dual mode; in shared mode background work steps aside).
            queued = time.perf_counter()
            with live_scheduler.live_turn(source=source_key(source, user_id), workload="look",
                                          timeout=LOCK_TIMEOUT_S, drop=True) as turn:
                faces = engine.detect_faces(working, timings, lock_timeout=turn.remaining())
            wait_ms = max((time.perf_counter() - queued) * 1000 - live_scheduler.inference_ms(timings), 0.0)
        except live_scheduler.LiveBusy as exc:
            raise _busy(f"{exc} (live queue)", exc.retry_after, _LIVE_REASONS.get(exc.reason, "queue_full")) from exc
        except engine.InferenceBusy as exc:
            raise _busy("Face inference is busy with other work; drop this frame.", exc.retry_after,
                        "model_busy") from exc
        except Exception as exc:  # noqa: BLE001 — a model/runtime failure is "unavailable", not a client error
            logger.exception("look: inference failed")
            raise LookError(503, "Recognition model is unavailable.") from exc
        # Per-source threshold override (robot hub F8), else the global value.
        threshold = settings_cache.get_threshold_for(clean_source(source))
        matches = (recognition_index.match_batch(np.stack([f.embedding for f in faces]), threshold)
                   if faces else [])
    finally:
        _release()

    faces_out = _faces_out(faces, matches, _addresses([matches]), scale, w, h)
    took_ms = round((time.perf_counter() - started) * 1000, 1)
    live_scheduler.look_stats.ok(wait_ms, took_ms)
    return {"image": {"w": int(w), "h": int(h)},
            "took_ms": took_ms,
            "kept": False,
            "threshold_used": threshold,
            "faces": faces_out}


MAX_BATCH = 8
MAX_BATCH_BODY_BYTES = MAX_BATCH * MAX_PHOTO_BYTES + 64 * 1024


def _item_error(index: int, source: str | None, exc: LookError) -> dict:
    item = {"index": index, "source": source, "status": exc.status, "detail": exc.detail}
    if exc.status == 429:
        item.update(limit=exc.limit, reason=exc.reason, retry_after_ms=exc.retry_after_ms)
    return item


def look_batch(items: list[tuple[bytes, str | None]], user_id: str | None = None) -> dict:
    """Several JPEGs (each with its own camera label) in ONE live turn:
    every frame is detected in turn (the installed detector takes one image per
    call) and all their faces are embedded in ONE batched recognition call.
    Same per-item result shape as look(); one bad or busy item never fails the
    others. Nothing is saved. Raises LookError(429) only when NO item could be
    answered because the service is busy, and LookError(503) when the model is
    not available."""
    started = time.perf_counter()
    results: list[dict | None] = [None] * len(items)
    valid = []
    for i, (data, source) in enumerate(items):
        try:
            _check_jpeg(data)
            valid.append(i)
        except LookError as exc:
            results[i] = _item_error(i, source, exc)
    if not engine.is_ready():
        raise LookError(503, "Recognition model is not loaded yet.")

    admitted = []
    for i in valid:
        if _admit():
            admitted.append(i)
        else:
            results[i] = _item_error(i, items[i][1], _busy("Too many look requests in progress; drop this frame.",
                                                          reason="inflight_full"))
    order: list[int] = []
    faces_lists: list = []
    frames: dict = {}
    matches_by: dict = {}
    thresholds: dict = {}
    wait_ms = 0.0
    try:
        for i in admitted:
            img = storage_service.decode_image(items[i][0])
            if img is None:
                results[i] = _item_error(i, items[i][1], LookError(400, "Could not decode the JPEG."))
                continue
            working, scale = engine.resize_for_detection(img)
            frames[i] = (img.shape[1], img.shape[0], working, scale)
        order = list(frames)
        if order:
            timings: dict = {}
            try:
                queued = time.perf_counter()
                with live_scheduler.live_turn(source=f"batch:{user_id or 'unknown'}", workload="look_batch",
                                              timeout=LOCK_TIMEOUT_S, drop=True, frames=len(order)) as turn:
                    faces_lists = engine.detect_faces_batch([frames[i][2] for i in order], timings,
                                                            lock_timeout=turn.remaining())
                wait_ms = max((time.perf_counter() - queued) * 1000 - live_scheduler.inference_ms(timings), 0.0)
            except live_scheduler.LiveBusy as exc:
                busy = _busy(f"{exc} (live queue)", exc.retry_after, _LIVE_REASONS.get(exc.reason, "queue_full"))
                for i in order:
                    results[i] = _item_error(i, items[i][1], busy)
                order = []
            except engine.InferenceBusy as exc:
                busy = _busy("Face inference is busy with other work; drop this frame.", exc.retry_after, "model_busy")
                for i in order:
                    results[i] = _item_error(i, items[i][1], busy)
                order = []
            except Exception as exc:  # noqa: BLE001
                logger.exception("look batch: inference failed")
                raise LookError(503, "Recognition model is unavailable.") from exc
        for i, faces in zip(order, faces_lists):
            thresholds[i] = settings_cache.get_threshold_for(clean_source(items[i][1]))
            matches_by[i] = (recognition_index.match_batch(np.stack([f.embedding for f in faces]), thresholds[i])
                             if faces else [])
    finally:
        for _ in admitted:
            _release()

    addresses = _addresses(matches_by.values())
    took_ms = round((time.perf_counter() - started) * 1000, 1)
    for i, faces in zip(order, faces_lists):
        w, h, _working, scale = frames[i]
        results[i] = {"index": i, "source": items[i][1], "status": 200, "image": {"w": int(w), "h": int(h)},
                      "took_ms": took_ms, "kept": False, "threshold_used": thresholds[i],
                      "faces": _faces_out(faces, matches_by[i], addresses, scale, w, h)}
        live_scheduler.look_stats.ok(wait_ms, took_ms)
    if results and all(r["status"] == 429 for r in results):
        first = results[0]
        raise LookError(429, "All frames in the batch were refused: the service is busy.",
                        retry_headers(first["retry_after_ms"] / 1000), limit=first["limit"], reason=first["reason"],
                        retry_after_ms=first["retry_after_ms"])
    return {"took_ms": took_ms, "kept": False, "items": results}
