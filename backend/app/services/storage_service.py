from __future__ import annotations

import uuid
from pathlib import Path

import io

import cv2
import numpy as np
from PIL import Image

from app.config import EVENTS_DIR, PEOPLE_DIR, STORAGE_PATH, THUMBNAILS_DIR

# iPhones photograph in HEIC by default, and OpenCV cannot read it at all —
# cv2.imdecode simply returns None, which every caller reports as "not a
# readable image". pillow_heif teaches Pillow the format; registering the
# opener once here is enough for Image.open() anywhere in the process.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001 — a missing/broken codec must not stop the app booting
    HEIF_SUPPORTED = False


def decode_image(file_bytes: bytes) -> np.ndarray | None:
    """Bytes -> BGR uint8 array, or None if this is not a readable image.

    OpenCV stays the primary decoder so nothing changes for the formats that
    already worked. Pillow is only consulted when OpenCV declines, which is
    where HEIC/HEIF (and anything else Pillow knows) gets picked up. Either
    way the caller receives the identical representation, so there is exactly
    one recognition path and HEIC is not special downstream.
    """
    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is not None:
        return img

    try:
        with Image.open(io.BytesIO(file_bytes)) as pil:
            # convert() also flattens any alpha channel, which the detector
            # would otherwise never see and cv2.IMREAD_COLOR would have dropped.
            rgb = np.array(pil.convert("RGB"))
    except Exception:  # noqa: BLE001 — unreadable is a normal outcome here, not an error
        return None
    if rgb.size == 0:
        return None
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def is_heif_bytes(file_bytes: bytes) -> bool:
    """True for an ISO-BMFF still image (HEIC/HEIF/AVIF), read from the file
    signature rather than the filename — an uploaded name can lie.

    Layout: 4-byte box size, b"ftyp", then the 4-byte major brand.
    """
    if len(file_bytes) < 12 or file_bytes[4:8] != b"ftyp":
        return False
    return file_bytes[8:12] in (
        b"heic", b"heix", b"heim", b"heis",   # HEIC
        b"hevc", b"hevx",                      # HEVC image sequence
        b"mif1", b"msf1",                      # generic HEIF
        b"avif", b"avis",                      # AVIF
    )


def to_displayable_bytes(file_bytes: bytes) -> tuple[bytes, str]:
    """Return (bytes, extension) safe to store as a participant's profile photo.

    Browsers cannot render HEIC, so a HEIC upload stored verbatim would show as
    a broken image everywhere in the app. Those are re-encoded to JPEG; every
    other format is returned untouched, byte for byte.

    This runs only when SAVING the visible photo. The embedding is always
    computed from decode_image() on the ORIGINAL bytes, before this, so
    re-encoding can never affect recognition.
    """
    if not is_heif_bytes(file_bytes):
        return file_bytes, ".jpg"

    img = decode_image(file_bytes)
    if img is None:
        return file_bytes, ".jpg"
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        return file_bytes, ".jpg"
    return buf.tobytes(), ".jpg"


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
