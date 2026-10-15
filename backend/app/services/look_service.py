"""Stateless "who is in this JPEG" for the robot hub (POST /api/look).

The hub on the same PC pulls the latest frame from its own ESP32-CAM MJPEG
stream and asks Central what it sees, at most a few times a second per
camera with one request in flight. Central only evaluates the frame:

    decode -> the SAME detector/session/lock and recognition index/threshold
    the kiosk uses -> answer

and keeps nothing. No image or thumbnail, no Upload/FaceDetection/Attendance
row, no live-feed event, no node presence, no per-source history. That is the
whole contract, so the module imports no database or storage writer; its
only database access is reading Person.call_as for the matched ids.

Resource bounds, all enforced before the expensive work:
    bytes      photo <= MAX_PHOTO_BYTES (the route's ASGI guard also caps the
               raw request body while it is still being received)
    format     JPEG only (SOI marker, PIL header says JPEG, EOI present)
    pixels     header dimensions checked before the full decode
    capacity   at most MAX_IN_FLIGHT looks at once; the shared inference lock
               is waited for at most LOCK_TIMEOUT_S. Either limit -> "busy",
               so the hub drops the frame instead of queueing a stale one.
"""
from __future__ import annotations

import io
import logging
import threading
import time

import numpy as np
from PIL import Image, UnidentifiedImageError

from sqlmodel import Session

from app.database.db import engine as db_engine
from app.face_recognition import engine
from app.face_recognition.index import recognition_index
from app.services import inference_priority, settings_cache, storage_service
from app.services.person_address import call_as_for

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 2 * 1024 * 1024
MAX_BODY_BYTES = MAX_PHOTO_BYTES + 64 * 1024  # multipart framing + small form fields
MAX_SIDE = 2048
MAX_PIXELS = 4_200_000
MAX_IN_FLIGHT = 4
LOCK_TIMEOUT_S = 0.5

_admission = threading.BoundedSemaphore(MAX_IN_FLIGHT)


class LookError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.headers = headers


def _busy(detail: str) -> LookError:
    return LookError(429, detail, {"Retry-After": "0"})


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


def look(data: bytes) -> dict:
    started = time.perf_counter()
    w, h = _check_jpeg(data)
    if not engine.is_ready():
        raise LookError(503, "Recognition model is not loaded yet.")
    if not _admission.acquire(blocking=False):
        raise _busy("Too many look requests in progress; drop this frame.")
    try:
        img = storage_service.decode_image(data)
        if img is None:
            raise LookError(400, "Could not decode the JPEG.")
        h, w = img.shape[:2]
        working, scale = engine.resize_for_detection(img)
        try:
            # Announce a live caller: a tagged-video render steps aside for it
            # between its own inference calls (services/inference_priority.py).
            with inference_priority.interactive():
                faces = engine.detect_faces(working, lock_timeout=LOCK_TIMEOUT_S)
        except engine.InferenceBusy as exc:
            raise _busy("Face inference is busy with other work; drop this frame.") from exc
        except Exception as exc:  # noqa: BLE001 — a model/runtime failure is "unavailable", not a client error
            logger.exception("look: inference failed")
            raise LookError(503, "Recognition model is unavailable.") from exc
        matches = (recognition_index.match_batch(np.stack([f.embedding for f in faces]),
                                                 settings_cache.get_threshold()) if faces else [])
    finally:
        _admission.release()

    matched_ids = [m[0] for m in matches if m[0]]
    addresses = {}
    if matched_ids:
        with Session(db_engine) as session:
            addresses = call_as_for(session, matched_ids)
    out = []
    for face, (person_id, first_name, full_name, participant_id, score) in zip(faces, matches):
        box = _box(face.bbox, scale, w, h)
        entry = {"box": box, "face_score": round(float(face.det_score), 4),
                 "status": "matched" if person_id else "unknown"}
        if person_id:
            entry.update(person_id=person_id, participant_id=participant_id, first_name=first_name,
                         name=full_name, call_as=addresses.get(person_id), confidence=round(float(score), 4))
        out.append((max(box[2] - box[0], 0.0) * max(box[3] - box[1], 0.0), entry))
    # Largest (usually nearest) face first; stable for equal areas.
    out.sort(key=lambda pair: -pair[0])
    return {"image": {"w": int(w), "h": int(h)},
            "took_ms": round((time.perf_counter() - started) * 1000, 1),
            "kept": False,
            "faces": [entry for _area, entry in out]}
