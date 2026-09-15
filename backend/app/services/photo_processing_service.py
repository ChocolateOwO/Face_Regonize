"""Event photo processing pipeline.

Reuses the exact existing face recognition stack — nothing about detection,
embedding, matching, or the threshold is reimplemented here:

  - app.face_recognition.engine.detect_faces()   (detection + embedding)
  - app.face_recognition.index.recognition_index  (vectorized matching)
  - app.services.settings_cache.get_threshold()   (same threshold as the kiosk)

Flow per photo: download -> decode -> detect_faces() -> recognition_index
.match_batch() -> classify (ambience / sorted) -> look up
each matched participant's CURRENT consent status -> blur only matched faces
with explicit declined consent. Unknown and pending faces remain visible.

Local processing is now the ONLY thing this pipeline does automatically:

  write ORIGINAL/SORTED/AMBIENCE/MEDIA under
  storage/photo_batches/{batch_id}/, commit the DB rows, then set
  status="ready" and STOP. This is what the in-app web preview reads, so it
  never depends on Google Drive being reachable, connected, or working.

Google Drive upload is a separate, explicit, user-initiated action — see
app.services.drive_destination_service (paste a destination folder URL,
validate it, then Upload). Nothing here calls Drive on completion.

Consent is resolved from the existing ConsentRecord table (same "latest
record wins" rule the PDPA feature uses) and then frozen into
PhotoBatchFace.consent_status_at_processing at that exact moment — nothing
ever re-reads live consent for an already-processed photo again, matching
"consent is final for processed photos."
"""
from __future__ import annotations

import logging
import mimetypes
import json
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from sqlmodel import Session, select

from app.config import PHOTO_BATCHES_DIR, STORAGE_PATH
from app.database.db import engine
from app.services.event_photo_detection_service import detect_event_faces as detect_faces
from app.face_recognition.index import recognition_index
from app.models.models import (
    CleanupLog,
    ConsentRecord,
    Person,
    PhotoBatch,
    PhotoBatchFace,
    PhotoBatchIngestionIssue,
    PhotoBatchParticipantFolder,
    PhotoBatchPhoto,
)
from app.services import storage_service
from app.services import settings_cache
from app.services import event_eta_service
from app.services import photo_thumbnail_service
from app.services.google_drive_folder_service import (
    DriveFolderError,
    download_file,
    extract_folder_id,
    get_folder_name,
    list_image_files,
)
from app.services.photo_source import DrivePhotoSource, LocalUploadPhotoSource
from app.services.google_drive_oauth_service import (
    DriveOAuthError,
    copy_file,
    create_root_folder,
    get_or_create_subfolder,
    upload_bytes,
)

logger = logging.getLogger(__name__)

_INVALID_FS_CHARS = '<>:"/\\|?*'
_LOGO_POSITIONS = {"top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"}
_batch_lock = threading.Condition()
_active_batches: set[str] = set()
_cancelled_batches: set[str] = set()


def _cancelled(batch_id: str) -> bool:
    with _batch_lock:
        return batch_id in _cancelled_batches


def cancel_and_delete_batch(batch_id: str, timeout_seconds: float = 30) -> bool:
    """Cancel at the next safe boundary, then remove only owned local state.
    STOP-before-DELETE for BOTH workflow versions: the existing
    _active_batches wait covers workflow_version=1 (and is also how
    workflow_version=2's own outer run_photo_batch call is tracked); the
    PipelineRegistry check additionally waits for the pipeline's own
    internal worker threads to actually finish, not just the outer call."""
    with _batch_lock:
        _cancelled_batches.add(batch_id)
        _batch_lock.wait_for(lambda: batch_id not in _active_batches, timeout=timeout_seconds)
        if batch_id in _active_batches:
            return False
    from app.services.event_pipeline_registry import registry as pipeline_registry

    if not pipeline_registry.shutdown_and_join_if_active(batch_id, timeout_seconds):
        return False
    _delete_owned_state(batch_id)
    return True


def _batch_root(batch_id: str) -> Path | None:
    """photo_batches/<id>/ — derived from the batch's OWN id (a 32-hex uuid),
    never from a stored path, so nothing here can ever reach shared storage."""
    import re

    if not re.fullmatch(r"[0-9a-f]{32}", batch_id or ""):
        return None
    return STORAGE_PATH / "photo_batches" / batch_id


def _delete_owned_state(batch_id: str) -> bool:
    """Remove ONE batch's own rows and its own storage directory. Never
    Person, embeddings, Attendance, ConsentRecord, Upload, another batch, the
    database file or shared storage. False if the row was already gone.
    Callers make sure nothing is running for the batch first."""
    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return False
        batch_dir = _batch_root(batch.id)
        if batch_dir is None:
            logger.error("Photo batch %r has a non-standard id; its files were left in place.", batch.id)
        elif batch_dir.exists():
            shutil.rmtree(batch_dir)
        for photo in session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all():
            for face in session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all(): session.delete(face)
            session.delete(photo)
        for folder in session.exec(select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch_id)).all(): session.delete(folder)
        _delete_review_rows(session, batch_id)
        from app.services import identity_candidate_service
        identity_candidate_service.delete_for_batch(session, batch_id)
        session.delete(batch); session.commit()
    return True


# ---- Pause / Resume, and a separate Delete ---------------------------------
# Built on the existing machinery only: _cancelled_batches (the cooperative
# flag every processing loop and pipeline stage checks at photo boundaries),
# _active_batches (run + retry), the PipelineRegistry handle, and the
# batch_edit_lock / Drive-upload reservations. Pause keeps every completed
# result and every pending item; Resume rebuilds the queue from the database.
# Statuses are plain strings ("pausing", "paused", "resuming") — no migration.

import os
import time as _time

ACTIVE_STATUSES = frozenset({"pending", "processing", "pausing", "resuming", "stopping"})
PAUSABLE_STATUSES = frozenset({"pending", "processing", "resuming"})
PAUSE_SLOW_SECONDS = 60
_pause_waiters: set[str] = set()
_pause_requested_at: dict[str, float] = {}
_heartbeats: dict[str, float] = {}
_TEMP_SUFFIXES = (".tmp", ".regen-tmp", ".part")


class BatchBusy(RuntimeError):
    """A delete that must not run yet. The message is shown to the admin."""


class ResumeRefused(RuntimeError):
    """A resume that must not run. The message is shown to the admin."""


def _worker_checkpoint(batch_id: str) -> bool:
    """The cancel check every worker already makes at a safe checkpoint, plus
    a heartbeat for the pause watchdog. True = stop taking new work."""
    with _batch_lock:
        _heartbeats[batch_id] = _time.time()
        return batch_id in _cancelled_batches


def batch_activity(batch_id: str) -> list[str]:
    """Everything currently touching a batch, as admin-readable phrases."""
    from app.services import batch_edit_lock
    from app.services import drive_destination_service as dds
    from app.services.event_pipeline_registry import registry as pipeline_registry

    reasons: list[str] = []
    with _batch_lock:
        if batch_id in _active_batches or batch_id in _pause_waiters:
            reasons.append("processing")
    if "processing" not in reasons and pipeline_registry.is_active(batch_id):
        reasons.append("processing")
    with dds._upload_lock:
        if batch_id in dds._uploading_batches:
            reasons.append("a Google Drive upload")
    with batch_edit_lock._lock:
        if batch_id in batch_edit_lock._editing:
            reasons.append("a face-box edit")
        if batch_edit_lock._downloads.get(batch_id, 0) > 0:
            reasons.append("a ZIP download")
    try:
        from app.services import identity_backfill_service as bf

        job = bf.status()
        if job.get("state") in ("starting", "running", "paused", "cancelling") and job.get("batch_id") in (None, batch_id):
            reasons.append("an identity-candidate backfill")
    except Exception:  # noqa: BLE001 — an unavailable backfill module never blocks anything
        pass
    return reasons


def _cleanup_abandoned_temp(batch_id: str) -> list[str]:
    """Only abandoned temporaries an interrupted write left behind (.tmp,
    .regen-tmp, .part — anywhere, staging included). The staged uploads
    themselves are the source for Resume and are kept; so are ORIGINAL,
    finalized MEDIA/SORTED/AMBIENCE/THUMBNAILS and every DB row."""
    root = _batch_root(batch_id)
    removed: list[str] = []
    if root is None or not root.is_dir():
        return removed
    for path in list(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name.endswith(_TEMP_SUFFIXES):
            path.unlink(missing_ok=True)
            removed.append(path.relative_to(root).as_posix())
    return removed


# ---- Resume instrumentation -------------------------------------------------
# Off unless RECONIZE_RESUME_TIMING=1, so a normal run writes nothing. When on,
# each resume appends JSON lines under the batch's own folder (never exported,
# deleted with the batch) — this is how the resume stages are measured.
_TIMING_ENV = "RECONIZE_RESUME_TIMING"


def _timing_enabled() -> bool:
    return os.getenv(_TIMING_ENV) == "1"


def _timing(batch_id: str, **fields) -> None:
    if not _timing_enabled():
        return
    try:
        root = _batch_root(batch_id)
        if root is None:
            return
        root.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now().isoformat(timespec="milliseconds"), **fields})
        with (root / "RESUME_TIMING.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — instrumentation never breaks a run
        logger.debug("Photo batch %s: could not write resume timing", batch_id, exc_info=True)


def _ms(since: float) -> float:
    return round((_time.perf_counter() - since) * 1000, 1)


def _warm_model_ms() -> float | None:
    """Measurement only: how long the recognition model takes to hand out.
    It stays loaded for the life of the process, so a resume after a pause
    pays nothing here; only a cold process pays the real load, once."""
    if not _timing_enabled():
        return None
    from app.face_recognition import engine as face_engine

    clock = _time.perf_counter()
    face_engine.get_face_app()
    return _ms(clock)


class _StagedItem:
    """A source item that is already staged locally under ORIGINAL/."""

    __slots__ = ("id", "name")

    def __init__(self, item_id: str, name: str):
        self.id, self.name = item_id, name


class _StagedFirstSource:
    """Resume source: a photo already promoted to ORIGINAL/ is read from disk;
    only an item that never arrived is fetched from the real source. Both are
    counted, so "Resume downloaded nothing" is a measurement, not a claim."""

    def __init__(self, original_dir: Path, inner, remote: bool):
        self._dir, self._inner, self._remote = original_dir, inner, remote
        self.staged_reads = 0
        self.source_downloads = 0

    def list_items(self):
        return self._inner.list_items()

    def fetch(self, item):
        local = self._dir / item.name
        if local.is_file():
            self.staged_reads += 1
            return local.read_bytes()
        data = self._inner.fetch(item)
        if self._remote:
            self.source_downloads += 1
        else:
            # A local-upload batch was staged on this machine before processing
            # ever started: reading that staging file is not a download.
            self.staged_reads += 1
        return data


def _already_handled(session: Session, batch_id: str) -> set[str]:
    """Source items a previous (paused) run already dealt with: a photo row
    (finished, or failed and left for Retry) or a recorded rejection. A
    resumed run never processes or counts these again."""
    names = {p.filename for p in session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all()}
    names |= {i.filename for i in session.exec(
        select(PhotoBatchIngestionIssue).where(PhotoBatchIngestionIssue.batch_id == batch_id)).all()}
    return names


def request_pause(batch_id: str) -> dict:
    """Cooperative pause. Idempotent: pausing/paused return the current state
    and never start a second pause task."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    with _batch_lock:
        with Session(engine) as session:
            batch = session.get(PhotoBatch, batch_id)
            if batch is None:
                return {"status": "not_found", "pausing": False}
            running = batch_id in _active_batches or pipeline_registry.is_active(batch_id)
            if batch.status == "paused" and not running:
                return {"status": "paused", "pausing": False}
            if batch.status == "pausing" and batch_id in _pause_waiters:
                return {"status": "pausing", "pausing": True}
            if not running and batch.status not in PAUSABLE_STATUSES | {"pausing"}:
                return {"status": batch.status, "pausing": False}
            _cancelled_batches.add(batch_id)
            if batch.status != "pausing":
                batch.status = "pausing"
                batch.current_stage = "Pausing — finishing the photo in progress, then pausing."
                session.add(batch)
                session.commit()
            _pause_requested_at.setdefault(batch_id, _time.time())
            first = batch_id not in _pause_waiters
            _pause_waiters.add(batch_id)
    handle = pipeline_registry.get(batch_id)
    if handle is not None:
        handle.cancel()
    if first:
        threading.Thread(target=_complete_pause, args=(batch_id,), name=f"pause-{batch_id[:8]}", daemon=True).start()
    return {"status": "pausing", "pausing": True}


def wait_for_pause(batch_id: str, timeout: float | None = None) -> bool:
    with _batch_lock:
        return _batch_lock.wait_for(lambda: batch_id not in _pause_waiters, timeout=timeout)


def wait_until_idle(batch_id: str, timeout: float = 60) -> bool:
    """Test/diagnostic helper: no run, retry, resume or pause task left."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    deadline = _time.time() + timeout
    with _batch_lock:
        ok = _batch_lock.wait_for(lambda: batch_id not in _active_batches and batch_id not in _pause_waiters,
                                  timeout=timeout)
    while ok and pipeline_registry.is_active(batch_id) and _time.time() < deadline:
        _time.sleep(0.05)
    return ok and not pipeline_registry.is_active(batch_id)


def _complete_pause(batch_id: str) -> None:
    """Waits until every worker of the run has exited — no timeout that could
    fake "paused" while something still writes — then removes only abandoned
    temporaries and marks the batch paused (resumable, never READY)."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    try:
        with _batch_lock:
            _batch_lock.wait_for(lambda: batch_id not in _active_batches)
        while not pipeline_registry.shutdown_and_join_if_active(batch_id, timeout=5):
            pass
        _cleanup_abandoned_temp(batch_id)
        with _batch_lock:
            with Session(engine) as session:
                batch = session.get(PhotoBatch, batch_id)
                if batch is not None:
                    accepted = max(0, batch.total_photos - batch.rejected_photos)
                    batch.status = "paused"
                    # Unfinished, resumable work — never READY. This does NOT
                    # exempt the batch from retention: run_retention_cleanup
                    # treats "paused" by its original deadline.
                    _set_new_axis_local_status(batch, "PROCESSING")
                    batch.current_stage = (f"Processing paused — {batch.processed_photos} of {accepted} photo(s) done. "
                                           "Resume to continue.")
                    session.add(batch)
                    session.commit()
            _cancelled_batches.discard(batch_id)  # Resume may run again
        event_eta_service.forget(batch_id)
    except Exception:  # noqa: BLE001 — leaves "pausing"; Pause again retries the completion
        logger.exception("Photo batch %s: could not complete Pause", batch_id)
    finally:
        with _batch_lock:
            _pause_waiters.discard(batch_id)
            _pause_requested_at.pop(batch_id, None)
            _batch_lock.notify_all()


def request_resume(batch_id: str) -> dict:
    """Continue a paused batch from DB state. Idempotent: a batch that is
    already resuming/processing is returned as-is — never a second run."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    started = _time.perf_counter()
    others = [r for r in batch_activity(batch_id) if r != "processing"]
    with _batch_lock:
        claim_clock = _time.perf_counter()
        with Session(engine) as session:
            batch = session.get(PhotoBatch, batch_id)
            if batch is None:
                return {"status": "not_found", "resuming": False}
            running = batch_id in _active_batches or pipeline_registry.is_active(batch_id)
            if running or batch.status in ("resuming", "processing", "pending"):
                return {"status": batch.status, "resuming": False}
            if batch.status == "pausing":
                raise ResumeRefused("The batch is still pausing. Wait until it shows \"Processing paused\", then resume.")
            if batch.status != "paused":
                raise ResumeRefused(f"Only a paused batch can be resumed (this batch is {batch.status}).")
            if others:
                raise ResumeRefused(f"Cannot resume while {' and '.join(others)} is running for this batch.")
            _cancelled_batches.discard(batch_id)
            batch.status = "resuming"
            batch.current_stage = f"Resuming — continuing after {batch.processed_photos} completed photo(s)."
            session.add(batch)
            session.commit()
            _active_batches.add(batch_id)  # claimed here, atomically: a second Resume sees it running
            claim_ms = _ms(claim_clock)
    # The run happens on its own thread: the endpoint returns as soon as the
    # state says "resuming".
    threading.Thread(target=_run_resumed, args=(batch_id,), name=f"resume-{batch_id[:8]}", daemon=True).start()
    _timing(batch_id, stage="resume_endpoint", resume_endpoint_ms=_ms(started), active_batch_claim_ms=claim_ms)
    return {"status": "resuming", "resuming": True}


def _run_resumed(batch_id: str) -> None:
    try:
        _run_photo_batch(batch_id)
    finally:
        with _batch_lock:
            _active_batches.discard(batch_id)
            _batch_lock.notify_all()


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None


def pause_diagnostics(batch_id: str, stage: str) -> dict:
    """Pause watchdog from existing worker state: when pause was requested,
    the last worker checkpoint, whether workers still run, and the stage."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    with _batch_lock:
        requested = _pause_requested_at.get(batch_id)
        beat = _heartbeats.get(batch_id)
        waiting = batch_id in _pause_waiters
        active = batch_id in _active_batches
    active = active or pipeline_registry.is_active(batch_id)
    now = _time.time()
    return {
        "pause_requested_at": _iso(requested),
        "last_heartbeat_at": _iso(beat),
        "seconds_since_heartbeat": round(now - beat) if beat else None,
        "workers_active": active,
        "stage": stage or "",
        "slow": bool(requested and now - requested > PAUSE_SLOW_SECONDS),
        "stale": not active and not waiting,
    }


def pause_flags(batch: PhotoBatch) -> dict:
    """What the page may offer right now. Diagnostics only while pausing."""
    from app.services.event_pipeline_registry import registry as pipeline_registry

    activity = batch_activity(batch.id)
    with _batch_lock:
        running = batch.id in _active_batches
        waiting = batch.id in _pause_waiters
    running = running or pipeline_registry.is_active(batch.id)
    stale_pausing = batch.status == "pausing" and not running and not waiting
    flags = {
        "can_pause": batch.status in PAUSABLE_STATUSES or stale_pausing,
        "can_resume": batch.status == "paused" and not activity,
        "can_delete": batch.status not in ACTIVE_STATUSES and not activity,
    }
    if batch.status == "pausing":
        flags["pause_diagnostics"] = pause_diagnostics(batch.id, batch.current_stage)
    return flags


def delete_batch(batch_id: str) -> dict:
    """Delete one paused or finished batch — a separate, explicit action.
    Refused while anything touches it; idempotent once deleted."""
    from app.services import batch_edit_lock

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if batch is None:
            return {"deleted": False, "already_deleted": True}
        status = batch.status
    if status == "pausing":
        raise BatchBusy("The batch is still pausing. Wait until it shows \"Processing paused\", then delete it.")
    if status in ACTIVE_STATUSES:
        raise BatchBusy("Processing is running. Press \"Pause processing\" and wait until it shows "
                        "\"Processing paused\", then delete.")
    reasons = batch_activity(batch_id)
    if reasons:
        raise BatchBusy(f"Cannot delete while {' and '.join(reasons)} is running for this batch. Try again when it finishes.")
    # Exclusive against a ZIP build, a face-box edit or a Drive upload starting now.
    if not batch_edit_lock.reserve_edit(batch_id):
        raise BatchBusy("A face-box edit, ZIP download or Google Drive upload just started for this batch. Try again when it finishes.")
    try:
        with _batch_lock:
            if batch_id in _active_batches or batch_id in _pause_waiters:
                raise BatchBusy("Processing started again for this batch. Pause it first.")
            _cancelled_batches.add(batch_id)  # every later run/retry/export refuses from here on
        deleted = _delete_owned_state(batch_id)
    finally:
        batch_edit_lock.release_edit(batch_id)
    return {"deleted": deleted, "already_deleted": not deleted}


def _delete_review_rows(session: Session, batch_id: str) -> int:
    """Legacy cleanup: the Review workflow is removed and nothing creates
    these rows any more, but a batch predating the removal may still carry
    some. A deleted batch must take them with it — decisions reference their
    item, so they go first — otherwise they would outlive the photos they
    describe. The migration-006 tables themselves stay in the schema, inert."""
    from app.models.models import PhotoBatchReviewDecision, PhotoBatchReviewItem

    items = session.exec(
        select(PhotoBatchReviewItem).where(PhotoBatchReviewItem.batch_id == batch_id)
    ).all()
    removed = 0
    for item in items:
        for decision in session.exec(
            select(PhotoBatchReviewDecision).where(PhotoBatchReviewDecision.review_item_id == item.id)
        ).all():
            session.delete(decision)
            removed += 1
        session.delete(item)
        removed += 1
    return removed


def _safe_folder_name(participant_id: str, first_name: str, last_name: str) -> str:
    raw = f"{participant_id}_{first_name}_{last_name}".strip().rstrip("_")
    return "".join(c if c not in _INVALID_FS_CHARS else "_" for c in raw)


def _consent_status(session: Session, person_id: str) -> str:
    """"consented" | "declined" | "pending" — the participant's current
    (latest) consent choice, resolved once at processing time. Distinct from
    the PDPA feature's own internal helper (not imported from there) so this
    feature has no code dependency on pdpa.py's internals."""
    record = session.exec(
        select(ConsentRecord).where(ConsentRecord.person_id == person_id).order_by(ConsentRecord.recorded_at.desc()).limit(1)
    ).first()
    return record.choice if record else "pending"


def _create_batch_row(
    session: Session,
    *,
    label: str,
    drive_folder_id: str,
    retention_days: int,
    user_id: str,
    source_type: str,
    logo_png: bytes | None,
    logo_position: str,
    logo_size: float,
    mask_style: str = "blur",
    mask_png: bytes | None = None,
    mask_source: str | None = None,
) -> PhotoBatch:
    """Shared by create_batch (Drive) and create_local_batch (Phase A2) —
    everything about a batch's row/directories/logo setup is source-agnostic;
    only the source_type value and drive_folder_id (empty for local uploads,
    since PhotoBatch.drive_folder_id is a plain non-nullable str with no
    Drive-specific meaning once source_type != "drive") differ."""
    if not (1 <= retention_days <= 7):
        raise ValueError("retention_days must be between 1 and 7")

    # Phase B: the ONE place workflow_version is decided, at creation —
    # never changed afterward. A batch already running keeps whichever
    # path it started on even if the setting flips mid-run.
    workflow_version = 2 if settings_cache.get_use_concurrent_pipeline() else 1

    batch = PhotoBatch(
        label=label,
        drive_folder_id=drive_folder_id,
        storage_dir="",  # filled in below once we have the id
        retention_days=retention_days,
        retention_start_at=datetime.now(),
        delete_at=datetime.now() + timedelta(days=retention_days),
        created_by=user_id,
        source_type=source_type,
        workflow_version=workflow_version,
    )
    session.add(batch)
    session.commit()
    session.refresh(batch)

    batch.storage_dir = f"photo_batches/{batch.id}"
    session.add(batch)
    session.commit()

    batch_dir = PHOTO_BATCHES_DIR / batch.id
    for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "MEDIA"):
        (batch_dir / sub).mkdir(parents=True, exist_ok=True)
    if logo_png:
        logo_dir = batch_dir / "LOGO"
        logo_dir.mkdir(exist_ok=True)
        _write_bytes(logo_dir / "logo.png", logo_png)
        (logo_dir / "config.json").write_text(json.dumps({"position": logo_position, "size": logo_size}), encoding="utf-8")

    # Phase D2 — privacy mask style, batch-local like LOGO. Only written when
    # an image style (built-in emoji or validated custom PNG, resolved by
    # mask_style_service before this is called) is chosen AND its PNG is
    # present; anything else leaves no config at all, which _load_mask_config
    # reads as "blur".
    if mask_style == "image" and mask_png:
        mask_dir = batch_dir / "MASK"
        mask_dir.mkdir(exist_ok=True)
        _write_bytes(mask_dir / "mask.png", mask_png)
        (mask_dir / "config.json").write_text(
            json.dumps({"style": "image", "source": mask_source or "custom"}), encoding="utf-8")

    return batch


def create_batch(session: Session, folder_url_or_id: str, retention_days: int, user_id: str, logo_png: bytes | None = None, logo_position: str = "bottom-right", logo_size: float = 0.15,
                 *, mask_style: str = "blur", mask_png: bytes | None = None, mask_source: str | None = None) -> PhotoBatch:
    folder_id = extract_folder_id(folder_url_or_id)
    label = get_folder_name(folder_id) or folder_id  # cosmetic — never fail a batch over the label
    return _create_batch_row(
        session, label=label, drive_folder_id=folder_id, retention_days=retention_days, user_id=user_id,
        source_type="drive",  # explicit — every batch created through this path is Drive-sourced (Phase A1)
        logo_png=logo_png, logo_position=logo_position, logo_size=logo_size,
        mask_style=mask_style, mask_png=mask_png, mask_source=mask_source,
    )


def create_local_batch(session: Session, label: str, retention_days: int, user_id: str, logo_png: bytes | None = None, logo_position: str = "bottom-right", logo_size: float = 0.15,
                       *, mask_style: str = "blur", mask_png: bytes | None = None, mask_source: str | None = None) -> PhotoBatch:
    """Phase A2 — creates a batch row for local file-upload sourcing. The
    caller (the upload API route) streams the actual uploaded files into
    this batch's UPLOAD_STAGING/ directory AFTER this returns (it needs the
    batch id first), then starts run_photo_batch() exactly like Drive."""
    return _create_batch_row(
        session, label=label, drive_folder_id="", retention_days=retention_days, user_id=user_id,
        source_type="local",
        logo_png=logo_png, logo_position=logo_position, logo_size=logo_size,
        mask_style=mask_style, mask_png=mask_png, mask_source=mask_source,
    )


def upload_staging_dir(batch_id: str) -> Path:
    return PHOTO_BATCHES_DIR / batch_id / "UPLOAD_STAGING"


def _encode(img: np.ndarray, filename: str) -> bytes:
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ".jpg"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        raise RuntimeError(f"cv2.imencode failed for {filename}")
    return buf.tobytes()


def _write_bytes(path: Path, data: bytes) -> None:
    """cv2.imwrite() silently fails (returns False, no exception) on Windows
    when the path contains non-ASCII characters, because it goes through a
    C-level fopen() that doesn't understand Unicode paths there — this
    project's own storage path does (Thai characters). Everything written
    here therefore goes through Python's own file handle, which has no such
    restriction; images are encoded to bytes first via _encode()."""
    path.write_bytes(data)


def _blur_region(img: np.ndarray, bbox: tuple[float, float, float, float]) -> None:
    """Blurs in place. Expands the tight detection bbox by a margin so hair/
    ears/forehead near the face are covered too, and uses a kernel large
    enough relative to the face that the result isn't reversible by simple
    sharpening."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    pad_x, pad_y = bw * 0.25, bh * 0.35
    ex1 = max(0, int(x1 - pad_x))
    ey1 = max(0, int(y1 - pad_y))
    ex2 = min(w, int(x2 + pad_x))
    ey2 = min(h, int(y2 + pad_y))
    if ex2 <= ex1 or ey2 <= ey1:
        return
    region = img[ey1:ey2, ex1:ex2]
    k = max(31, (min(region.shape[0], region.shape[1]) // 2) | 1)  # odd kernel, scales with face size
    blurred = cv2.GaussianBlur(region, (k, k), 0)
    blurred = cv2.GaussianBlur(blurred, (k, k), 0)  # two passes — stronger, harder to reverse
    img[ey1:ey2, ex1:ex2] = blurred


def _load_mask_config(batch_dir: Path) -> tuple[str, np.ndarray | None]:
    """Phase D1 — per-batch privacy mask style, same batch-local lifecycle as
    LOGO (written once at create_batch, read once at batch-run start).

    Returns ("blur", None) for anything it cannot read or does not recognise.
    That fallback is deliberate and load-bearing: a mask config that fails to
    parse must degrade to the STRONGER default, never to "no mask" — the
    faces this covers are participants who explicitly declined consent.
    """
    try:
        config = json.loads((batch_dir / "MASK" / "config.json").read_text(encoding="utf-8"))
        style = str(config.get("style", "blur")).lower()
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return "blur", None
    if style != "image":
        return "blur", None
    try:
        mask = cv2.imdecode(
            np.frombuffer((batch_dir / "MASK" / "mask.png").read_bytes(), dtype=np.uint8),
            cv2.IMREAD_UNCHANGED,
        )
    except OSError:
        return "blur", None
    if mask is None or mask.ndim != 3 or mask.shape[2] not in (3, 4):
        return "blur", None
    return "image", mask


def _apply_mask_image(img: np.ndarray, bbox: tuple[float, float, float, float], mask: np.ndarray) -> bool:
    """Alpha-composite `mask` over one face box. Mirrors _apply_logo's blend
    math, anchored to a face instead of an image corner, and covers the SAME
    padded region _blur_region does so the two styles conceal equally much.

    Returns False if it could not cover the face, so the caller can fall back
    to blur rather than leaving a declined participant visible.
    """
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    pad_x, pad_y = bw * 0.25, bh * 0.35
    ex1 = max(0, int(x1 - pad_x))
    ey1 = max(0, int(y1 - pad_y))
    ex2 = min(w, int(x2 + pad_x))
    ey2 = min(h, int(y2 + pad_y))
    if ex2 <= ex1 or ey2 <= ey1:
        return False
    target_w, target_h = ex2 - ex1, ey2 - ey1
    try:
        resized = cv2.resize(mask, (target_w, target_h), interpolation=cv2.INTER_AREA)
    except cv2.error:
        return False
    overlay = resized[:, :, :3].astype(np.float32)
    alpha = (
        resized[:, :, 3:4].astype(np.float32) / 255.0 if resized.shape[2] == 4
        else np.ones((target_h, target_w, 1), dtype=np.float32)
    )
    region = img[ey1:ey2, ex1:ex2].astype(np.float32)
    img[ey1:ey2, ex1:ex2] = (overlay * alpha + region * (1 - alpha)).astype(np.uint8)
    return True


def _apply_privacy_mask(img: np.ndarray, bbox, mask_config: tuple[str, np.ndarray | None] | None) -> None:
    """The ONE place a face gets concealed. The decision of WHETHER to conceal
    is made by the caller and is unchanged by this phase — this only dispatches
    on style, and always conceals by one means or another.

    Phase D2 — fail-closed compositing: the padded region is ALWAYS blurred
    first, and an image style is composited over the blur. A transparent or
    partially transparent mask pixel can therefore only reveal blur, and any
    compositing error leaves the blur in place. The blur-only path is
    byte-identical to before."""
    style, mask = mask_config or ("blur", None)
    _blur_region(img, bbox)
    if style == "image" and mask is not None:
        try:
            _apply_mask_image(img, bbox, mask)
        except Exception:  # noqa: BLE001 — the face is already blurred; never fail open or fail the photo
            logger.warning("Privacy mask image could not be composited; blur kept", exc_info=True)


def _load_logo_config(batch_dir: Path) -> tuple[np.ndarray, str, float] | None:
    try:
        config = json.loads((batch_dir / "LOGO" / "config.json").read_text(encoding="utf-8"))
        position, size = config["position"], float(config["size"])
        logo = cv2.imdecode(np.frombuffer((batch_dir / "LOGO" / "logo.png").read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if position not in _LOGO_POSITIONS or not 0.02 <= size <= 0.5 or logo is None or logo.ndim != 3 or logo.shape[2] not in (3, 4):
            return None
        return logo, position, size
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _apply_logo(media: np.ndarray, logo_config: tuple[np.ndarray, str, float] | None) -> None:
    if not logo_config:
        return
    logo, position, relative_size = logo_config
    height, width = media.shape[:2]
    target_width = max(1, int(width * relative_size))
    target_height = max(1, int(logo.shape[0] * target_width / logo.shape[1]))
    if target_width > width or target_height > height:
        return
    logo = cv2.resize(logo, (target_width, target_height), interpolation=cv2.INTER_AREA)
    pad = max(4, int(min(width, height) * 0.02))
    x = pad if position.endswith("left") else width - target_width - pad if position.endswith("right") else (width - target_width) // 2
    y = pad if position.startswith("top") else height - target_height - pad
    overlay = logo[:, :, :3].astype(np.float32)
    alpha = (logo[:, :, 3:4].astype(np.float32) / 255.0) if logo.shape[2] == 4 else np.ones((target_height, target_width, 1), dtype=np.float32)
    region = media[y:y + target_height, x:x + target_width].astype(np.float32)
    media[y:y + target_height, x:x + target_width] = (overlay * alpha + region * (1 - alpha)).astype(np.uint8)


def _drive_upload_photo(
    session: Session,
    batch: PhotoBatch,
    photo: PhotoBatchPhoto,
    *,
    file_bytes: bytes,
    media_bytes: bytes,
    mime_type: str,
    media_folder_id: str,
    extra_folder_ids: list[str],
) -> None:
    """STAGE 2: mirror one already-locally-saved photo into Google Drive.

    Every upload/copy is verified by google_drive_oauth_service (the file id
    is read back and its parent folder confirmed) — an upload is only ever
    recorded as successful once that check passes. Any failure is caught
    here, recorded on the photo and the batch, and never propagated: the
    local result and the web preview must survive a broken Drive.
    """
    try:
        # The unmodified original is uploaded once, then server-side-copied
        # into every other folder it belongs in — the same bytes never leave
        # this app more than once.
        original_copy_id: str | None = None
        for folder_id in extra_folder_ids:
            if not folder_id:
                continue
            if original_copy_id is None:
                original_copy_id = upload_bytes(folder_id, photo.filename, file_bytes, mime_type)
            else:
                copy_file(original_copy_id, folder_id, photo.filename)

        if media_folder_id:
            photo.media_drive_file_id = upload_bytes(media_folder_id, photo.filename, media_bytes, mime_type)

        photo.drive_upload_status = "uploaded"
        photo.drive_error = None
    except Exception as e:  # noqa: BLE001 — Drive must never break the local result
        logger.exception("Photo batch %s: Drive upload failed for %s", batch.id, photo.filename)
        photo.drive_upload_status = "failed"
        photo.drive_error = str(e)
        batch.drive_failed_photos += 1
        batch.drive_error = f"{photo.filename}: {e}"
    finally:
        session.add(photo)
        session.add(batch)
        session.commit()


def run_photo_batch(batch_id: str) -> None:
    with _batch_lock: _active_batches.add(batch_id)
    try:
        _run_photo_batch(batch_id)
    finally:
        with _batch_lock:
            _active_batches.discard(batch_id); _batch_lock.notify_all()


def _run_photo_batch(batch_id: str) -> None:
    # An application update is mid-flight; its database backup has already
    # been taken, so anything written now could be silently discarded by a
    # rollback. The batch stays pending and can be started again afterwards.
    from app.services import maintenance

    if maintenance.is_active():
        logger.info("Photo batch %s deferred: an application update is in progress.", batch_id)
        return

    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return
        with _batch_lock:
            # Task 2 — a stopped batch never starts again: not after a Stop
            # pressed while it was queued, and not after a restart (which
            # empties _cancelled_batches but not the persisted status).
            if batch_id in _cancelled_batches or batch.status in ("cancelled", "stopping", "pausing", "paused"):
                return
            resuming = batch.status == "resuming"
            batch.status = "processing"
            if batch.workflow_version >= 2:
                batch.local_status = "PROCESSING"
            batch.current_stage = "Listing Drive folder..." if batch.source_type == "drive" else "Listing uploaded files..."
            # Phase G4 — the persisted counterpart of the in-memory ETA state, so
            # the final duration survives a refresh/restart. Cleared here so a
            # re-run never shows a stale "finished" time while it is running.
            # A resumed run keeps its original start.
            if not resuming or batch.processing_started_at is None:
                batch.processing_started_at = datetime.now()
            batch.processing_finished_at = None
            event_eta_service.start(batch_id)
            session.add(batch)
            session.commit()

        batch_dir = PHOTO_BATCHES_DIR / batch.id
        for sub in ("ORIGINAL", "SORTED", "AMBIENCE", "MEDIA"):
            (batch_dir / sub).mkdir(parents=True, exist_ok=True)
        logo_config = _load_logo_config(batch_dir)
        mask_config = _load_mask_config(batch_dir)

        threshold = settings_cache.get_threshold()

        # Phase A1: routed through PhotoSource, but list_image_files/
        # download_file are passed in BY NAME from this module's own
        # namespace — a behavior-preserving wrapper, not a new call path
        # (see photo_source.py's docstring for why this keeps existing
        # tests, which patch pps.list_image_files/pps.download_file,
        # working unmodified). Phase A2: a source_type="local" batch was
        # already fully staged by the upload API route before this
        # background task ever starts — LocalUploadPhotoSource just reads
        # what's already on disk under UPLOAD_STAGING/.
        if batch.source_type == "local":
            source = LocalUploadPhotoSource(upload_staging_dir(batch.id))
        else:
            source = DrivePhotoSource(batch.drive_folder_id, list_fn=list_image_files, fetch_fn=download_file)

        # Resume — continue from DB state: never process or count again an
        # item the paused run already finished, failed (left for Retry) or
        # rejected. Two bulk queries, never one per photo. Empty for a fresh run.
        clock = _time.perf_counter()
        handled = _already_handled(session, batch.id)
        handled_ms = _ms(clock)

        original_dir = batch_dir / "ORIGINAL"
        staged_names = ({e.name for e in os.scandir(original_dir) if e.is_file()}
                        if original_dir.is_dir() else set())

        # A resume reuses what staging already produced. When every source item
        # is already staged (or already handled), the SOURCE is not consulted at
        # all: no Drive listing, no download. A half-staged batch still needs the
        # listing to learn which items never arrived — but every file that IS
        # staged is read from ORIGINAL/ instead of being downloaded again.
        clock = _time.perf_counter()
        fully_staged = bool(resuming and batch.total_photos > 0
                            and len(handled | staged_names) >= batch.total_photos)
        if fully_staged:
            files = [_StagedItem(name, name) for name in sorted(handled | staged_names)]
        else:
            try:
                files = source.list_items()
            except DriveFolderError as e:
                batch.status = "failed"
                batch.current_stage = str(e)
                session.add(batch)
                session.commit()
                return
            # The listing is authoritative whenever it ran — including a
            # resume of a batch that was paused before it ever listed.
            batch.total_photos = len(files)
            session.add(batch)
            session.commit()
        listing_ms = _ms(clock)

        if resuming:
            source = _StagedFirstSource(original_dir, source, remote=batch.source_type != "local")

        clock = _time.perf_counter()
        todo = [f for f in files if f.name not in handled]
        done_before = len(files) - len(todo)
        if resuming:
            _timing(batch.id, stage="resume_queue", handled_state_query_ms=handled_ms, handled_state_queries=2,
                    source_listing_ms=listing_ms, source_listed=not fully_staged, queue_build_ms=_ms(clock),
                    pending_photo_count=len(todo), staged_file_count=len(staged_names),
                    model_load_ms=_warm_model_ms())

        if batch.workflow_version == 2:
            batch_storage_dir = batch.storage_dir
            batch_id_for_paths = batch.id
            session.close()  # the pipeline opens its own sessions per stage
            _run_photo_batch_pipeline(batch_id, source, todo, batch_storage_dir, batch_id_for_paths, logo_config, threshold, mask_config)
            _timing(batch_id, stage="source_counts", new_download_count=getattr(source, "source_downloads", 0),
                    existing_staged_count=getattr(source, "staged_reads", 0))
            return

        # ---- PHASE 1: local processing -------------------------------------
        # Drive READS (list_image_files above, download_file below) are the
        # batch's input and stay. Drive WRITES do not happen here at all: an
        # upload used to sit between one photo's recognition and the next, so
        # network latency delayed face detection that needed nothing from it.
        _timing(batch_id, stage="workers", worker_start_ms=0.0, queued_items=len(todo))
        for index, f in enumerate(todo, start=done_before + 1):
            if _worker_checkpoint(batch_id):
                _timing(batch_id, stage="source_counts", new_download_count=getattr(source, "source_downloads", 0),
                        existing_staged_count=getattr(source, "staged_reads", 0))
                return
            batch.current_stage = f"Processing photos — {index} of {len(files)}: {f.name}"
            session.add(batch)
            session.commit()

            # ---- pre-acceptance: download, decode, validate, promote to
            # ORIGINAL/. A failure here NEVER produced an accepted photo —
            # it is a permanent rejection, reported by name, and can never
            # be retried into existence (see retry_unresolved_photos, which
            # only ever re-reads photos that already reached ORIGINAL/).
            try:
                file_bytes = source.fetch(f)
                img = storage_service.decode_image(file_bytes)
                if img is None:
                    raise RuntimeError("not a readable image")
                original_rel = f"{batch.storage_dir}/ORIGINAL/{f.name}"
                _write_bytes(STORAGE_PATH / original_rel, file_bytes)
            except Exception as e:  # noqa: BLE001 — one bad source item must not kill the whole batch
                logger.exception("Photo batch %s: rejected %s before acceptance", batch.id, f.name)
                batch.rejected_photos += 1
                session.add(PhotoBatchIngestionIssue(batch_id=batch.id, filename=f.name, reason=str(e)))
                session.add(batch)
                session.commit()
                continue

            # ACCEPTED — a real PhotoBatchPhoto row exists from this point on,
            # so a later failure is always retriable (failed_photos), never a
            # silently-dropped photo.
            photo = PhotoBatchPhoto(
                batch_id=batch.id,
                filename=f.name,
                drive_file_id=f.id,
                original_path=original_rel,
            )
            session.add(photo)
            session.commit()
            session.refresh(photo)

            try:
                # Counted processed inside the photo's own single finalize
                # commit (Amendment A1) — all or nothing, nothing to do here.
                _process_accepted_photo(session, batch, photo, img, file_bytes, logo_config, threshold, mask_config)
            except Exception as e:  # noqa: BLE001 — one bad photo must not kill the whole batch
                logger.exception("Photo batch %s: failed to process %s", batch.id, f.name)
                batch.failed_photos += 1
                batch.last_error = f"{f.name}: {e}"
                batch.current_stage = f"Skipped {f.name}: {e}"
                session.add(batch)
                session.commit()
            finally:
                # Both outcomes consumed wall-clock and reduced what is left.
                event_eta_service.record_photo_done(batch_id)

        # Phase 1 is done: every local output exists and the web preview is
        # fully usable from here on. STOP here — Google Drive upload is now a
        # separate, explicit, user-initiated action (see
        # app.services.drive_destination_service), never automatic.
        with _batch_lock:
            # Task 2 — a Stop that arrived during the last photo wins: a stopped
            # batch is never marked ready. Same lock request_pause registers under.
            if batch_id in _cancelled_batches:
                return
            _finalize_batch_status(batch)
            session.add(batch)
            session.commit()


def _set_new_axis_local_status(batch: PhotoBatch, new_local_status: str) -> None:
    """Phase M2 — writes the migration-004 local_status axis, authoritative
    for retention ONLY once workflow_version>=2 (see run_retention_cleanup).
    workflow_version=1 batches never have this called for them from
    _finalize_batch_status's callers below, but the guard is kept here too
    as a second, cheap line of defense — M1's legacy-status-based
    protection for those batches must never depend on this axis at all.

    delete_at is NEVER touched here. Retention counts from when the retention
    setting was applied — batch creation, or a later change_retention — so the
    deadline an admin sees is the deadline that holds all the way through
    Processing → READY, and through Processing → Pause → Resume → READY. A
    pause, a resume or a retry can move when READY happens; none of them may
    move when the batch expires. (This used to be recomputed on the first
    transition to READY, which let a paused batch's deadline slide forward by
    however long it stayed paused.)
    """
    if batch.workflow_version < 2:
        return
    batch.local_status = new_local_status


def _finalize_batch_status(batch: PhotoBatch) -> None:
    """The phase-end status decision, shared verbatim by BOTH the sequential
    path and the pipeline path (Phase B), so the two can never drift apart
    on what "done" means. `ready` requires failed_photos==0 — a batch with
    unresolved photos is NOT internally complete and must not advance; the
    admin retries via retry-unresolved instead. rejected_photos alone never
    blocks this (those items can never be retried into existence) UNLESS
    every single discovered item was rejected, in which case there is no
    accepted photo at all to call "ready".

    Also drives the Phase M2 local_status axis (workflow_version>=2 only) —
    one call site, so the legacy `status` string and the new axis can never
    disagree about what just happened.
    """
    accepted_photos = batch.total_photos - batch.rejected_photos
    if accepted_photos <= 0:
        batch.status = "failed"
        batch.current_stage = f"All {batch.total_photos} photo(s) were rejected — nothing to process."
        _set_new_axis_local_status(batch, "FAILED")
    elif batch.failed_photos == 0:
        # "ready" means the local output is internally COMPLETE. That is
        # decided solely by the Output Completeness Invariant — there is no
        # human-review condition: the Review workflow was removed from the
        # product, so nothing else can hold a complete batch back.
        batch.status = "ready"
        batch.current_stage = ""
        _set_new_axis_local_status(batch, "READY")
    else:
        batch.status = "needs_retry"
        batch.current_stage = f"{batch.failed_photos} photo(s) failed — see last error below."
        # Still incomplete, not a terminal state — mapped to PROCESSING
        # (not a new "NEEDS_RETRY" value) so it falls under the exact same
        # retention protection PROCESSING already gets; migration 004's
        # local_status enum predates N1's needs_retry concept and adding a
        # new persisted value for it is out of scope for this phase.
        _set_new_axis_local_status(batch, "PROCESSING")

    # Phase A2: once the main pass has reached any of the three outcomes
    # above, every accepted local-upload photo already has its own durable
    # copy under ORIGINAL/ — retry re-reads THAT, never the staging area
    # (see retry_unresolved_photos), so the staged copies are now pure
    # redundant disk use for however long the batch's retention window
    # lasts. Best-effort: a cleanup failure must never fail batch finalization.
    if batch.source_type == "local":
        shutil.rmtree(upload_staging_dir(batch.id), ignore_errors=True)

    # Phase G4 — one place decides "the pass is over", so the persisted
    # duration and the in-memory ETA can never disagree about when that was.
    # Set for every outcome (ready / needs_retry / failed): the run really did
    # stop here, and a later retry starts a fresh window.
    batch.processing_finished_at = datetime.now()
    event_eta_service.forget(batch.id)


def _run_photo_batch_pipeline(
    batch_id: str, source, files: list, batch_storage_dir: str, batch_id_for_paths: str, logo_config, threshold: float,
    mask_config=None,
) -> None:
    """Phase B: delegates the per-photo work to EventPhotoPipeline (staged,
    concurrent), then applies the SAME phase-end status decision the
    sequential path uses. Deferred import — see event_pipeline_service's
    module docstring for why (breaks a circular import between the two
    modules)."""
    from app.services.event_pipeline_service import EventPhotoPipeline

    items = [(i, f, f.name) for i, f in enumerate(files, start=1)]

    def _progress(message: str) -> None:
        with Session(engine) as session:
            batch = session.get(PhotoBatch, batch_id)
            if batch:
                batch.current_stage = message
                session.add(batch)
                session.commit()

    clock = _time.perf_counter()
    pipeline = EventPhotoPipeline(
        batch_id, source, logo_config, threshold, is_cancelled=_worker_checkpoint, progress=_progress, mask_config=mask_config,
        storage_path=STORAGE_PATH, photo_batches_dir=PHOTO_BATCHES_DIR, db_engine=engine,
    )
    _timing(batch_id, stage="workers", worker_start_ms=_ms(clock), queued_items=len(items))
    pipeline.run(items, batch_storage_dir, batch_id_for_paths)

    with _batch_lock:
        # Task 2 — checked and finalized under the lock request_pause uses.
        if batch_id in _cancelled_batches:
            return
        with Session(engine) as session:
            batch = session.get(PhotoBatch, batch_id)
            if not batch:
                return
            _finalize_batch_status(batch)
            session.add(batch)
            session.commit()


def _replace_face_rows(session: Session, photo: PhotoBatchPhoto, rows: list[PhotoBatchFace]) -> None:
    """Delete-and-insert this photo's face rows.

    Only ever called inside the photo's single finalize commit (Amendment A1),
    so a failed attempt never erases or half-modifies the rows an earlier
    attempt left behind, and a retry can never add a duplicate set.
    """
    for old in session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all():
        session.delete(old)
    for row in rows:
        row.photo_id = photo.id
        session.add(row)


def _commit_finalized(session: Session, batch: PhotoBatch, photo: PhotoBatchPhoto, apply, retry: bool) -> None:
    """The ONE commit that makes a photo count (Amendment A1).

    `apply` mutates the face rows, photo fields and batch counters; this adds
    the processed/failed bookkeeping and commits all of it together. On any
    failure the transaction is rolled back, so the previous face rows and every
    counter stay exactly as they were, and the session is left clean for the
    caller's own failure handling.
    """
    try:
        apply()
        if retry:
            batch.failed_photos -= 1
        batch.processed_photos += 1
        session.add(photo)
        session.add(batch)
        session.commit()
    except Exception:
        session.rollback()
        raise


def _process_accepted_photo(session, batch, photo, img, file_bytes, logo_config, threshold, mask_config=None,
                            *, retry: bool = False) -> None:
    """Detect/match/classify/render for ONE already-accepted photo (its bytes
    are already safely on disk under ORIGINAL/ — this function never writes
    there). Raises on any failure.

    The ordering is the whole correctness story (Amendment A1):
      1. compute everything — detection, matching, consent, render — with no
         database change at all;
      2. write every derived file (SORTED copies, AMBIENCE, MEDIA);
      3. ONE commit: this photo's face rows (delete-and-insert), its fields,
         participant-folder counts, every batch counter, and the
         processed/failed bookkeeping (`retry=True` moves the photo from
         failed_photos to processed_photos; otherwise it is simply counted
         processed).

    A failure anywhere before or during step 3 leaves the previous face rows
    and counters exactly as they were, so a retried photo can never be counted
    twice or gain duplicate face rows. This is the one place a photo's
    processed/failed transition happens, for the main loop and retry alike.
    """
    from app.services import identity_candidate_service

    faces = detect_faces(img)

    if not faces:
        # AMBIENCE: no faces, so nothing to blur — logo only (if configured).
        # Same finalized bytes fan out to BOTH destinations — one render,
        # not a raw copy for one and a real render for the other.
        media_img = img.copy()
        _apply_logo(media_img, logo_config)
        finalized_bytes = _encode(media_img, photo.filename) if logo_config else file_bytes
        ambience_rel = f"{batch.storage_dir}/AMBIENCE/{photo.filename}"
        media_rel = f"{batch.storage_dir}/MEDIA/{photo.filename}"
        _write_bytes(STORAGE_PATH / ambience_rel, finalized_bytes)
        _write_bytes(STORAGE_PATH / media_rel, finalized_bytes)

        def apply_ambience() -> None:
            _replace_face_rows(session, photo, [])
            identity_candidate_service.replace_candidates(session, batch.id, photo.id, [])
            photo.faces_total = 0
            photo.faces_matched = 0
            photo.faces_unknown = 0
            photo.classification = "ambience"
            photo.media_path = media_rel
            batch.ambience_photos += 1

        _commit_finalized(session, batch, photo, apply_ambience, retry)
        # One grid thumbnail per photo, from the FINALIZED bytes. Best-effort:
        # the grid falls back to MEDIA if this fails.
        photo_thumbnail_service.write_thumbnail(
            STORAGE_PATH / photo_thumbnail_service.thumbnail_relpath(batch.storage_dir, photo.filename),
            finalized_bytes,
        )
        return

    embeddings = np.stack([face.embedding for face in faces])
    matches = recognition_index.match_batch(embeddings, threshold)

    # STEP 1 — compute. Nothing in this step touches the database.
    matched_person_ids: set[str] = set()
    face_rows: list[PhotoBatchFace] = []
    for face, (person_id, _first, _full, _pid, score) in zip(faces, matches):
        if person_id:
            matched_person_ids.add(person_id)
            consent = _consent_status(session, person_id)
        else:
            consent = "no_match"
        face_rows.append(
            PhotoBatchFace(
                photo_id="",  # set in _replace_face_rows, inside the finalize commit
                person_id=person_id,
                confidence=score,
                bbox=",".join(f"{v:.1f}" for v in face.bbox),
                consent_status_at_processing=consent,
            )
        )
    faces_unknown = sum(1 for r in face_rows if r.consent_status_at_processing == "no_match")
    # Phase F1 — evaluation candidates (never trusted, never used to match),
    # computed here with no DB write and saved in the finalize commit below.
    candidates = identity_candidate_service.build_candidates(
        engine=engine, batch_id=batch.id, filename=photo.filename, img=img, faces=faces,
        predicted=[r.person_id for r in face_rows], threshold=threshold,
    )

    # RENDER ONCE — blur only a matched participant's explicit denial.
    # Consented, pending and unmatched faces stay visible. Apply the
    # decision independently to each face, then the logo, then encode. The
    # SAME finalized bytes are fanned out to every applicable destination
    # below (MEDIA always, SORTED per matched person) — never a second
    # render and never a raw ORIGINAL copy in any of them (that was the live
    # privacy gap: a declined participant's unmasked face could reach
    # another participant's SORTED folder untouched).
    media_img = img.copy()
    blurred_count = 0
    for row in face_rows:
        if row.person_id and row.consent_status_at_processing == "declined":
            bbox = tuple(float(v) for v in row.bbox.split(","))
            _apply_privacy_mask(media_img, bbox, mask_config)
            row.blurred = True
            blurred_count += 1

    _apply_logo(media_img, logo_config)
    finalized_bytes = _encode(media_img, photo.filename)

    # STEP 2 — FAN OUT (plain byte-copy, not hardlink — see Derived
    # Output/Logo Architecture: hardlinks would trip the download path's
    # st_nlink>1 safety check). One copy per recognized participant under
    # SORTED/ — a photo with several recognized people is copied into each of
    # their folders; the ORIGINAL is never touched by any of this. There is no
    # REVIEW/ destination any more. Every file lands before any DB change.
    sorted_person_ids: list[str] = []
    for person_id in matched_person_ids:
        person = session.get(Person, person_id)
        if not person:
            continue
        folder_name = _safe_folder_name(person.participant_id, person.first_name, person.last_name)
        dest_dir = PHOTO_BATCHES_DIR / batch.id / "SORTED" / folder_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        _write_bytes(dest_dir / photo.filename, finalized_bytes)
        sorted_person_ids.append(person_id)

    media_rel = f"{batch.storage_dir}/MEDIA/{photo.filename}"
    _write_bytes(STORAGE_PATH / media_rel, finalized_bytes)

    # STEP 3 — the single finalize commit.
    def apply_faces() -> None:
        _replace_face_rows(session, photo, face_rows)
        identity_candidate_service.replace_candidates(session, batch.id, photo.id, candidates,
                                                      [r.id for r in face_rows])
        photo.faces_total = len(faces)
        photo.faces_matched = len(matched_person_ids)
        photo.faces_unknown = faces_unknown
        # An unrecognized face no longer routes the photo anywhere special: the
        # Review workflow was removed from the product, so "review" is never
        # assigned. How many faces went unidentified is still visible per photo
        # (faces_unknown) and per batch, which is the part that was ever useful.
        photo.classification = "sorted"
        photo.media_path = media_rel
        if matched_person_ids:
            # Photos containing at least one identified participant — a photo
            # count, incremented exactly once per finalized photo.
            batch.recognized_photos += 1
        for person_id in sorted_person_ids:
            pf = session.exec(
                select(PhotoBatchParticipantFolder).where(
                    PhotoBatchParticipantFolder.batch_id == batch.id,
                    PhotoBatchParticipantFolder.person_id == person_id,
                )
            ).first()
            if not pf:
                pf = PhotoBatchParticipantFolder(batch_id=batch.id, person_id=person_id, folder_id="")
            pf.photo_count += 1
            session.add(pf)
        batch.faces_detected += len(faces)
        batch.faces_recognized += len(matched_person_ids)
        batch.faces_unknown += faces_unknown
        batch.blurred_faces += blurred_count
        consented_count = sum(1 for r in face_rows if r.consent_status_at_processing == "consented")
        batch.consented_faces += consented_count
        batch.not_consented_faces += len(face_rows) - consented_count

    _commit_finalized(session, batch, photo, apply_faces, retry)

    # Phase I4/J1 — ONE grid thumbnail per photo, written alongside (never
    # inside) the fan-out destinations, and derived from the FINALIZED bytes
    # so a declined participant's masked face stays masked in the grid too.
    # Best-effort: the grid falls back to full resolution if this fails.
    photo_thumbnail_service.write_thumbnail(
        STORAGE_PATH / photo_thumbnail_service.thumbnail_relpath(batch.storage_dir, photo.filename),
        finalized_bytes,
    )

def retry_unresolved_photos(batch_id: str) -> None:
    with _batch_lock: _active_batches.add(batch_id)
    try:
        _retry_unresolved_photos(batch_id)
    finally:
        with _batch_lock:
            _active_batches.discard(batch_id); _batch_lock.notify_all()


def _retry_unresolved_photos(batch_id: str) -> None:
    """Re-attempts ONLY photos counted in failed_photos — a PhotoBatchPhoto
    row that was accepted (has original_path) but never finalized (no
    media_path). Always re-reads its own ORIGINAL/{filename}, never the
    Drive source (which may have moved or been deleted since the batch
    started) — the same guarantee a fresh run gets. Never touches
    rejected_photos items; those have no PhotoBatchPhoto row at all and can
    never be retried into existence."""
    from app.services import maintenance

    if maintenance.is_active():
        logger.info("Retry for photo batch %s deferred: an application update is in progress.", batch_id)
        return

    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return
        unresolved = session.exec(
            select(PhotoBatchPhoto).where(
                PhotoBatchPhoto.batch_id == batch.id,
                PhotoBatchPhoto.media_path.is_(None),
            )
        ).all()
        if not unresolved:
            return

        logo_config = _load_logo_config(PHOTO_BATCHES_DIR / batch.id)
        mask_config = _load_mask_config(PHOTO_BATCHES_DIR / batch.id)
        threshold = settings_cache.get_threshold()

        with _batch_lock:
            if batch_id in _cancelled_batches or batch.status in ("cancelled", "stopping", "pausing", "paused"):
                return
            batch.status = "processing"
            if batch.workflow_version >= 2:
                batch.local_status = "PROCESSING"
            batch.current_stage = f"Retrying {len(unresolved)} unresolved photo(s)..."
            # Phase G4 — a retry is its own processing run; timing it from here
            # keeps the reported duration to work actually done rather than
            # spanning however long the batch sat waiting for someone to click.
            batch.processing_started_at = datetime.now()
            batch.processing_finished_at = None
            session.add(batch)
            session.commit()

        for index, photo in enumerate(unresolved, start=1):
            if _worker_checkpoint(batch_id): return
            batch.current_stage = f"Retrying photo {index} of {len(unresolved)}: {photo.filename}"
            session.add(batch)
            session.commit()

            try:
                file_bytes = (STORAGE_PATH / photo.original_path).read_bytes()
                img = storage_service.decode_image(file_bytes)
                if img is None:
                    raise RuntimeError("ORIGINAL file is no longer a readable image")
                # failed -> processed happens inside the photo's single
                # finalize commit (Amendment A1): all or nothing.
                _process_accepted_photo(session, batch, photo, img, file_bytes, logo_config, threshold, mask_config,
                                        retry=True)
            except Exception as e:  # noqa: BLE001 — one still-bad photo must not stop the retry pass
                logger.exception("Photo batch %s: retry failed for %s", batch.id, photo.filename)
                batch.last_error = f"{photo.filename}: {e}"
                batch.current_stage = f"Retry failed for {photo.filename}: {e}"
                session.add(batch)
                session.commit()

        # Phase M2: reuses the SAME finalize function the main run uses (it
        # was previously duplicated inline here with slightly different
        # wording) — one place decides "done", so the new local_status axis
        # and delete_at's first-READY timing can never drift between the
        # main run and a later retry that finally reaches ready.
        with _batch_lock:
            if batch_id in _cancelled_batches:
                return
            _finalize_batch_status(batch)
            session.add(batch)
            session.commit()


# ---- Legacy automatic Drive mirror (kept, no longer called automatically) --
# _sync_batch_to_drive/_drive_upload_photo implemented the OLD behaviour of
# mirroring every batch into a NEW folder this app created at the Drive root,
# immediately and automatically after phase 1. That automatic call has been
# removed (see the "ready" status above) in favour of the explicit, user-
# chosen destination flow in drive_destination_service.py. These two
# functions are intentionally left in place, unused by the automatic
# pipeline, only because test_photo_batch_download.py exercises them
# directly; they are safe to delete in a follow-up cleanup if no longer
# wanted.
def _sync_batch_to_drive(batch_id: str) -> None:
    """PHASE 2 — mirror an already locally-processed batch into Google Drive.

    File sync ONLY. No detection, embedding, matching or consent evaluation
    happens here; every one of those decisions was made in phase 1 and is read
    back from the database. Re-running any of it would be both wasteful and a
    way for two runs to disagree.

    Nothing local is ever deleted, rewritten or re-classified by this function.
    A batch whose Drive sync fails entirely still has all of its local output
    and still previews correctly — that is the whole point of the split.
    """
    with Session(engine) as session:
        if _cancelled(batch_id): return
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            return

        batch.status = "syncing_drive"
        batch.current_stage = "Preparing Drive output folders..."
        session.add(batch)
        session.commit()

        # Output folders are created HERE, not at batch start: creating them is
        # a Drive write, and phase 1 must contain none.
        try:
            # drive.file scope: this app may only touch what it created, so the
            # output tree is rooted in the connected admin's own Drive rather
            # than inside the photographer's folder.
            root_id = create_root_folder(f"Reconize — {batch.label} — {datetime.now():%Y-%m-%d %H%M}")
            processed_id = get_or_create_subfolder(root_id, "PROCESSED")
            media_id = get_or_create_subfolder(processed_id, "MEDIA")
            ambience_id = get_or_create_subfolder(processed_id, "AMBIENCE")
            review_id = get_or_create_subfolder(processed_id, "REVIEW")
            batch.processed_folder_id = processed_id
            batch.media_folder_id = media_id
            batch.ambience_folder_id = ambience_id
            batch.review_folder_id = review_id
            session.add(batch)
            session.commit()
        except DriveOAuthError as e:
            # Connected at batch start but not usable now — an expired or
            # revoked authorisation is the usual cause, and saying so is more
            # useful than the raw API error.
            logger.warning("Photo batch %s: Drive authorisation unusable: %s", batch_id, e)
            batch.drive_error = (
                f"Google Drive authorisation failed ({e}) — processed photos were saved locally only. "
                "Reconnect Google Drive in Settings."
            )
            batch.status = "completed"
            batch.current_stage = ""
            session.add(batch)
            session.commit()
            return
        except Exception as e:  # noqa: BLE001 — Drive must never cost local results
            logger.exception("Photo batch %s: Drive output folders unavailable", batch_id)
            batch.drive_error = f"Drive output unavailable: {e}"
            batch.status = "completed"
            batch.current_stage = ""
            session.add(batch)
            session.commit()
            return

        # Only photos not yet mirrored. A row already marked "failed" is left
        # alone rather than silently retried, so its error stays visible.
        photos = session.exec(
            select(PhotoBatchPhoto).where(
                PhotoBatchPhoto.batch_id == batch_id,
                PhotoBatchPhoto.drive_upload_status == "pending",
            ).order_by(PhotoBatchPhoto.id)
        ).all()

        participant_folder_ids: dict[str, str] = {}  # person_id -> Drive folder id

        for index, photo in enumerate(photos, start=1):
            if _cancelled(batch_id): return
            batch.current_stage = f"Syncing to Google Drive — {index} of {len(photos)}: {photo.filename}"
            session.add(batch)
            session.commit()

            try:
                # Read the bytes back off disk rather than carrying every photo
                # of the batch in memory — a large batch would be gigabytes.
                original_abs = STORAGE_PATH / photo.original_path if photo.original_path else None
                media_abs = STORAGE_PATH / photo.media_path if photo.media_path else None
                if not original_abs or not original_abs.exists():
                    raise RuntimeError("local original is missing — nothing to sync")
                file_bytes = original_abs.read_bytes()
                media_bytes = media_abs.read_bytes() if media_abs and media_abs.exists() else file_bytes

                # Phase 1 knew the Drive-reported mime type, but storing it
                # would need a schema change; the extension is what Drive
                # itself derives it from anyway.
                mime_type = mimetypes.guess_type(photo.filename)[0] or "image/jpeg"

                # Rebuild the destination list from what phase 1 recorded.
                extra: list[str] = []
                if photo.classification == "ambience":
                    extra.append(ambience_id)
                else:
                    person_ids = [
                        r.person_id
                        for r in session.exec(
                            select(PhotoBatchFace).where(
                                PhotoBatchFace.photo_id == photo.id,
                                PhotoBatchFace.person_id.is_not(None),
                            )
                        ).all()
                    ]
                    seen: set[str] = set()
                    for person_id in person_ids:
                        if person_id in seen:
                            continue
                        seen.add(person_id)
                        folder_id = participant_folder_ids.get(person_id)
                        if not folder_id:
                            person = session.get(Person, person_id)
                            if not person:
                                continue
                            folder_id = get_or_create_subfolder(
                                processed_id,
                                _safe_folder_name(person.participant_id, person.first_name, person.last_name),
                            )
                            participant_folder_ids[person_id] = folder_id
                            pf = session.exec(
                                select(PhotoBatchParticipantFolder).where(
                                    PhotoBatchParticipantFolder.batch_id == batch.id,
                                    PhotoBatchParticipantFolder.person_id == person_id,
                                )
                            ).first()
                            if pf:
                                pf.folder_id = folder_id
                                session.add(pf)
                        extra.append(folder_id)
                    if photo.classification == "review":
                        extra.append(review_id)
                    session.commit()

                # Unchanged: uploads the source bytes once, then server-side
                # copies into the remaining folders.
                _drive_upload_photo(
                    session, batch, photo,
                    file_bytes=file_bytes, media_bytes=media_bytes, mime_type=mime_type,
                    media_folder_id=media_id, extra_folder_ids=extra,
                )
            except Exception as e:  # noqa: BLE001 — one photo must not stop the sync
                logger.exception("Photo batch %s: Drive sync failed for %s", batch_id, photo.filename)
                photo.drive_upload_status = "failed"
                photo.drive_error = str(e)
                batch.drive_failed_photos += 1
                batch.drive_error = f"{photo.filename}: {e}"
                session.add(photo)
                session.add(batch)
                session.commit()

        batch.status = "completed"
        batch.current_stage = ""
        session.add(batch)
        session.commit()


def change_retention(session: Session, batch: PhotoBatch, new_retention_days: int) -> PhotoBatch:
    if not (1 <= new_retention_days <= 7):
        raise ValueError("retention_days must be between 1 and 7")
    batch.retention_days = new_retention_days
    batch.delete_at = batch.retention_start_at + timedelta(days=new_retention_days)
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def run_retention_cleanup() -> int:
    """Deletes every PhotoBatch whose delete_at has passed — its local
    storage directory (the web-preview copies) and its PhotoBatchPhoto/
    PhotoBatchFace/PhotoBatchParticipantFolder rows, plus the PhotoBatch row
    itself. Never touches Person, Attendance, FaceDetection, Upload, or any
    other batch, and never touches the batch's Google Drive PROCESSED folder
    — that stays in the photographer's Drive as the durable deliverable;
    only this app's own local copies and records expire. Writes one
    CleanupLog entry per deleted batch. Returns the number cleaned up."""
    # Never delete during an update window: the deletion would not be captured
    # by the pre-update database backup, so a rollback would resurrect the DB
    # rows while the files were already gone.
    from app.services import maintenance

    if maintenance.is_active():
        return 0

    # Never delete a batch something is still actively/meaningfully engaged
    # with: an in-flight or failed Drive upload (deleting local output out
    # from under it would strand the upload or destroy the only copy after
    # a failure the admin hasn't seen yet), or a batch awaiting
    # retry-unresolved (its failed photos are recoverable work, not junk).
    #
    # Phase M2: workflow_version>=2 batches are governed by the split
    # local_status/drive_status axes instead of the combined legacy
    # `status` string — a batch's workflow_version is set once, at
    # creation, and never changes, so no batch is ever evaluated by both
    # sets of rules. workflow_version=1 behavior (M1) is completely
    # unchanged below.
    _PROTECTED_LEGACY_STATUSES = ("uploading", "upload_failed", "needs_retry")
    _PROTECTED_LOCAL_STATUSES = ("PROCESSING", "REVIEW_REQUIRED")
    _PROTECTED_DRIVE_STATUSES = ("UPLOADING", "UPLOAD_FAILED")

    from app.services.event_pipeline_registry import registry as pipeline_registry

    def _is_protected(b: PhotoBatch) -> bool:
        # A paused batch is NOT exempt: it keeps the deadline it already had
        # and expires like any other batch. Only a live worker or an actual
        # reservation defers it, and both are checked below (a batch that is
        # pausing or resuming is still registered there, so it is covered).
        # Its unfinished local_status must therefore not protect it forever.
        if b.status == "paused":
            return b.workflow_version >= 2 and b.drive_status in _PROTECTED_DRIVE_STATUSES
        if b.workflow_version >= 2:
            return b.local_status in _PROTECTED_LOCAL_STATUSES or b.drive_status in _PROTECTED_DRIVE_STATUSES
        return b.status in _PROTECTED_LEGACY_STATUSES

    cleaned = 0
    with Session(engine) as session:
        now = datetime.now()
        # No status pre-filter in SQL: which field is authoritative depends
        # on workflow_version, a per-row branch _is_protected() already
        # handles — fetching every expired batch and filtering in Python
        # keeps that ONE decision in ONE place instead of two diverging
        # WHERE-clause shapes.
        expired = session.exec(select(PhotoBatch).where(PhotoBatch.delete_at <= now)).all()
        for batch in expired:
            # Re-check immediately before deleting: status could have
            # changed between the query above and now (e.g. a Drive upload
            # just started in a concurrent request).
            session.refresh(batch)
            if _is_protected(batch):
                continue
            # The registry is authoritative over "is something actively
            # touching this batch's files right now" for pipeline batches —
            # never delete regardless of any status field if so.
            if pipeline_registry.is_active(batch.id):
                continue
            # Follow-up Task 2 — never delete under a Stop in progress, a retry,
            # a Drive upload, a face-box edit or a ZIP build.
            if batch_activity(batch.id):
                continue
            status = "success"
            note = "Local copies and processing records deleted — the batch's Google Drive PROCESSED folder was not touched."
            try:
                if batch.storage_dir:
                    batch_dir = STORAGE_PATH / batch.storage_dir
                    if batch_dir.exists():
                        shutil.rmtree(batch_dir)
            except Exception as e:  # noqa: BLE001 — still remove the DB records even if file cleanup partially fails
                status = "partial"
                note = f"File cleanup issue: {e}"

            photos = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch.id)).all()
            photo_count = len(photos)
            face_count = 0
            for photo in photos:
                faces = session.exec(select(PhotoBatchFace).where(PhotoBatchFace.photo_id == photo.id)).all()
                face_count += len(faces)
                for face in faces:
                    session.delete(face)
                session.delete(photo)

            folders = session.exec(
                select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch.id)
            ).all()
            for folder in folders:
                session.delete(folder)

            review_rows = _delete_review_rows(session, batch.id)
            from app.services import identity_candidate_service
            candidate_rows = identity_candidate_service.delete_for_batch(session, batch.id)
            records_deleted = photo_count + face_count + len(folders) + review_rows + candidate_rows + 1  # +1 for the batch row itself
            session.add(CleanupLog(
                batch_id=batch.id,
                batch_label=batch.label,
                retention_days=batch.retention_days,
                photos_deleted=photo_count,
                records_deleted=records_deleted,
                status=status,
                note=note,
            ))
            session.delete(batch)
            session.commit()
            cleaned += 1
    return cleaned
