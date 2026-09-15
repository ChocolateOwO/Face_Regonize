"""Phase D1 — arbitration between a geometry edit and the things that READ a
batch's derived files.

A bounding-box edit replaces MEDIA / SORTED / AMBIENCE / THUMBNAILS files for
one photo. Anything reading those files at the same moment could capture a mix
of old and new output: a ZIP being built, or a Drive upload walking the
folders. One in-process lock guards all three kinds of activity so their
admission checks are atomic with respect to each other:

  * an edit is refused while a ZIP build or a Drive upload holds the batch;
  * a ZIP build is refused while an edit holds the batch (many ZIP builds may
    run at once — they only read);
  * a Drive upload reservation is refused while an edit holds the batch.

Processing and retry are excluded separately, through `batch_is_live` (the
edit gate refuses a live batch); a retry only ever touches photos that never
finalized, which an edit cannot target.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_editing: set[str] = set()
_downloads: dict[str, int] = {}


def reserve_edit(batch_id: str) -> bool:
    from app.services import drive_destination_service as dds

    with _lock:
        with dds._upload_lock:
            uploading = batch_id in dds._uploading_batches
        if batch_id in _editing or _downloads.get(batch_id, 0) > 0 or uploading:
            return False
        _editing.add(batch_id)
        return True


def release_edit(batch_id: str) -> None:
    with _lock:
        _editing.discard(batch_id)


def is_editing(batch_id: str) -> bool:
    with _lock:
        return batch_id in _editing


def begin_download(batch_id: str) -> bool:
    with _lock:
        if batch_id in _editing:
            return False
        _downloads[batch_id] = _downloads.get(batch_id, 0) + 1
        return True


def end_download(batch_id: str) -> None:
    with _lock:
        n = _downloads.get(batch_id, 0) - 1
        if n > 0:
            _downloads[batch_id] = n
        else:
            _downloads.pop(batch_id, None)


def try_reserve_upload(batch_id: str) -> bool:
    """`drive_destination_service.reserve_upload`, but refused while an edit
    holds the batch — checked under the same lock the edit reservation uses, so
    there is no window between the check and the reservation."""
    from app.services.drive_destination_service import reserve_upload

    with _lock:
        if batch_id in _editing:
            return False
        return reserve_upload(batch_id)
