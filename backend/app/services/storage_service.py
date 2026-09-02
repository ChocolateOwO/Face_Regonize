from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import EVENTS_DIR, PEOPLE_DIR, STORAGE_PATH, THUMBNAILS_DIR


def decode_image(file_bytes: bytes) -> np.ndarray | None:
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def save_person_image(participant_id: str, file_bytes: bytes, ext: str = ".jpg") -> str:
    """Saves under storage/people/{participant_id}/profile.ext, returns path relative to STORAGE_PATH."""
    person_dir = PEOPLE_DIR / participant_id
    person_dir.mkdir(parents=True, exist_ok=True)
    file_path = person_dir / f"profile{ext}"
    file_path.write_bytes(file_bytes)
    return str(file_path.relative_to(STORAGE_PATH))


def save_event_image(file_bytes: bytes, filename: str) -> tuple[str, str]:
    """Saves under storage/events/{yyyy-mm-dd}/{uuid}_{filename}.
    Returns (relative_path, thumbnail_relative_path).
    """
    from datetime import date

    day_dir = EVENTS_DIR / date.today().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "upload.jpg"
    unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    file_path = day_dir / unique_name
    file_path.write_bytes(file_bytes)

    thumb_path = THUMBNAILS_DIR / unique_name
    try:
        img = Image.open(file_path)
        img.thumbnail((320, 320))
        img.convert("RGB").save(thumb_path, "JPEG", quality=85)
        thumb_rel = str(thumb_path.relative_to(STORAGE_PATH))
    except Exception:
        thumb_rel = str(file_path.relative_to(STORAGE_PATH))

    return str(file_path.relative_to(STORAGE_PATH)), thumb_rel


def read_stored_file(relative_path: str) -> bytes:
    return (STORAGE_PATH / relative_path).read_bytes()


def storage_abs_path(relative_path: str) -> Path:
    return STORAGE_PATH / relative_path
