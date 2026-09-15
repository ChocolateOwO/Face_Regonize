"""Grid thumbnails for Event Photo batches (Phase I4 / J1).

Measured problem: the batch grid rendered the full-resolution artifact for
every tile. Real files average ~5.4 MB, so one 50-photo page pulled ~270 MB
and decoded full 3600x2400 frames — `loading="lazy"` only defers that, it does
not avoid it. A 320px JPEG is roughly 200x smaller.

Two rules this module exists to enforce:

  1. A thumbnail is ALWAYS derived from the finalized, privacy-rendered
     artifact — never from ORIGINAL/. A thumbnail of the original would put a
     declined participant's unmasked face straight into the grid.
  2. ONE thumbnail per photo, in a central THUMBNAILS/ folder — never one per
     SORTED person folder, which would duplicate the same image N times for a
     photo containing N recognized participants.

Thumbnails are derived and disposable: they are generated during the fan-out
write step for new batches, and on demand (then cached on disk) for batches
that predate this, so no backfill is needed. They are never an export
category.

Freshness (Phase D1): a photo can be re-rendered after it was finalized (a
bounding-box edit). Every write is an atomic replace through a UNIQUE temp
name, writers of the same thumbnail are serialised by a per-path lock, an
on-demand generation regenerates a thumbnail older than its MEDIA source and
throws its own result away if MEDIA changed while it was encoding, and the URL
is versioned by MEDIA's mtime_ns + size — so a stale tile can neither be
recreated on disk nor served from a browser cache after a re-render.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import uuid
from pathlib import Path

from PIL import Image

from app.services.fs_util import replace_with_retry

logger = logging.getLogger(__name__)

THUMBNAIL_DIR_NAME = "THUMBNAILS"
THUMBNAIL_MAX_SIZE = (320, 320)
THUMBNAIL_QUALITY = 85

_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def thumbnail_lock(path: Path) -> threading.Lock:
    """One lock per thumbnail file, shared by every writer of it (processing,
    on-demand generation, re-rendering)."""
    key = os.path.normcase(str(Path(path).resolve()))
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = threading.Lock()
        return lock


def thumbnail_relpath(batch_storage_dir: str, filename: str) -> str:
    """Storage-relative path, keyed by the photo's filename — the same key the
    finalized artifact uses, so every view resolves to the identical file."""
    return f"{batch_storage_dir}/{THUMBNAIL_DIR_NAME}/{filename}.jpg"


def encode_thumbnail(rendered_bytes: bytes) -> bytes | None:
    """JPEG thumbnail bytes from ALREADY-RENDERED bytes, or None if they cannot
    be read."""
    try:
        img = Image.open(io.BytesIO(rendered_bytes))
        img.thumbnail(THUMBNAIL_MAX_SIZE)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=THUMBNAIL_QUALITY)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — a derived file is never worth a failure
        logger.exception("Could not encode a thumbnail")
        return None


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: two writers of the same thumbnail never share a temp.
    tmp = dest.parent / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        # Retried: on Windows a reader of the old thumbnail (or a scanner)
        # briefly blocks replacing it.
        replace_with_retry(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def write_thumbnail(dest: Path, rendered_bytes: bytes) -> bool:
    """Encode and atomically write one thumbnail from ALREADY-RENDERED bytes.
    Returns False (never raises) if that is not possible: a missing thumbnail
    degrades the grid to full resolution, which is slow but correct, whereas
    failing the whole photo write would not be."""
    data = encode_thumbnail(rendered_bytes)
    if data is None:
        return False
    try:
        with thumbnail_lock(dest):
            _atomic_write(dest, data)
        return True
    except OSError:
        logger.exception("Could not write thumbnail %s", dest)
        return False


def is_thumbnail_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return parts[:1] == ["photo_batches"] and THUMBNAIL_DIR_NAME in parts


def source_relpath(thumbnail_rel: str) -> str | None:
    """The FINALIZED artifact a thumbnail is derived from.

    `photo_batches/<id>/THUMBNAILS/<name>.jpg` -> `photo_batches/<id>/MEDIA/<name>`.
    Deliberately resolves to MEDIA and never to ORIGINAL: a thumbnail of the
    original would put a declined participant's unmasked face in the grid.
    Returns None for anything not shaped like a thumbnail path.
    """
    parts = thumbnail_rel.replace("\\", "/").split("/")
    if len(parts) != 4 or parts[0] != "photo_batches" or parts[2] != THUMBNAIL_DIR_NAME:
        return None
    name = parts[3]
    if not name.endswith(".jpg"):
        return None
    return f"{parts[0]}/{parts[1]}/MEDIA/{name[: -len('.jpg')]}"


def ensure_thumbnail(storage_path: Path, thumbnail_rel: str) -> bool:
    """Make sure a FRESH thumbnail exists on disk, generating it if needed.

    Called from the file route rather than from the photo LIST endpoint on
    purpose: generating a whole page inline made the first request for a
    pre-existing batch take ~6s for 50 photos; doing it per image lets the
    browser's parallel image requests spread that cost.

    Fresh means "not older than its MEDIA source". A stale thumbnail (MEDIA was
    re-rendered after it) is regenerated. If MEDIA changes while this is
    encoding, the result is thrown away rather than written, so a stale tile can
    never be recreated by a race with a re-render.
    """
    dest = storage_path / thumbnail_rel
    source_rel = source_relpath(thumbnail_rel)
    if not source_rel:
        return dest.is_file()
    source = storage_path / source_rel
    with thumbnail_lock(dest):
        if not source.is_file():
            return dest.is_file()
        before = source.stat()
        if dest.is_file() and dest.stat().st_mtime_ns >= before.st_mtime_ns:
            return True
        data = encode_thumbnail(source.read_bytes())
        if data is None:
            return dest.is_file()
        after = source.stat()
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            return dest.is_file()
        _atomic_write(dest, data)
        return True


def thumbnail_url(batch_storage_dir: str, filename: str, storage_path: Path,
                  rendered_relpath: str | None) -> str | None:
    """The grid's `src`, WITHOUT touching or generating the thumbnail itself.

    Versioned by the mtime_ns + size of the artifact the thumbnail is derived
    from — a thumbnail is a pure function of those bytes, so a re-rendered photo
    yields a new URL even within the same second. That is what makes long-lived
    immutable caching safe: after a bounding-box edit the browser asks for a
    different URL instead of reusing a tile that may show a face the new render
    masks.

    None when there is no finalized artifact to derive from; the grid then
    falls back to MEDIA (and never to ORIGINAL).
    """
    if not rendered_relpath:
        return None
    try:
        st = (storage_path / rendered_relpath).stat()
    except OSError:
        return None
    return f"{thumbnail_relpath(batch_storage_dir, filename)}?v={st.st_mtime_ns}-{st.st_size}"
