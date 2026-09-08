"""Manual Google Drive destination — an admin pastes an existing Drive folder
URL, Reconize validates it is reachable under the existing OAuth write
identity, and (only on an explicit Upload click) mirrors the batch's already-
processed local SORTED and MEDIA output into a freshly named subfolder inside
it.

Deliberately independent of the local processing pipeline
(app.services.photo_processing_service): this reads only files that already
exist on local disk under storage/photo_batches/{id}/SORTED and .../MEDIA.
No detection, recognition, consent evaluation, blur or logo compositing runs
here — every one of those decisions was already made and written to disk by
the time this module ever touches a batch.

Authorization note: the OAuth write identity uses the `drive.file` scope
(see google_drive_oauth_service.py), which only grants access to files and
folders that identity itself created — NOT an arbitrary folder a user later
pastes a link to, even if the connected Google account can see it in the
normal Drive UI. Validation therefore fails with a clear message for most
pre-existing folders unless they were created by this app (e.g. a previous
batch's own upload-run folder pasted back in). Widening this would mean
requesting a broader Drive scope, which is out of scope for this feature —
see get_write_service()/SCOPES in google_drive_oauth_service.py.
"""
from __future__ import annotations

import mimetypes
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session

from app.config import STORAGE_PATH
from app.database.db import engine
from app.models.models import PhotoBatch
from app.services.google_drive_oauth_service import (
    DriveOAuthError,
    create_subfolder,
    get_or_create_subfolder,
    get_write_service,
    upload_bytes,
)
from app.services.photo_batch_download_service import _safe_path, batch_output_root, local_output_ready

import logging

logger = logging.getLogger(__name__)


class DriveDestinationError(Exception):
    pass


_FOLDER_PATH_RE = re.compile(r"^/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]{10,100})/?$")
_RAW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,100}$")
_ALLOWED_HOSTS = {"drive.google.com"}


def parse_destination_folder_url(value: str) -> str:
    """Strict parser for a Google Drive FOLDER destination. Accepts a full
    https://drive.google.com/drive/folders/<id> (optionally .../u/<n>/... and
    a query string) or a bare folder id typed directly. Rejects everything
    else, including file links, other hosts, and malformed input — and never
    treats the value as a local filesystem path."""
    raw = (value or "").strip()
    if not raw:
        raise DriveDestinationError("Paste a Google Drive folder URL.")

    if "://" not in raw:
        if _RAW_ID_RE.match(raw):
            return raw
        raise DriveDestinationError(
            "Enter a full Google Drive folder URL, e.g. https://drive.google.com/drive/folders/<id>."
        )

    try:
        parsed = urlparse(raw)
    except ValueError as e:
        raise DriveDestinationError("Malformed URL.") from e

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise DriveDestinationError("Malformed URL — a full Google Drive folder link is required.")
    if parsed.hostname not in _ALLOWED_HOSTS:
        raise DriveDestinationError(f"Not a Google Drive link (host: {parsed.hostname}).")

    match = _FOLDER_PATH_RE.match(parsed.path)
    if match:
        return match.group(1)
    if parsed.path.startswith("/file/"):
        raise DriveDestinationError("This links to a file, not a folder. Paste a folder link (.../drive/folders/<id>).")
    raise DriveDestinationError("Could not find a folder ID in this Google Drive URL — use a folder share link.")


def validate_destination(folder_id: str) -> dict:
    """Uses the EXISTING authenticated Drive write client — never expands
    scope. Confirms the folder exists, is really a folder (not trashed, not
    a file), and that the connected account can add files to it. Raises
    DriveDestinationError with a clear message otherwise; never returns
    tokens or credentials."""
    try:
        service = get_write_service()
    except DriveOAuthError as e:
        raise DriveDestinationError(str(e)) from e

    try:
        meta = (
            service.files()
            .get(fileId=folder_id, fields="id,name,mimeType,trashed,capabilities(canAddChildren)")
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — Drive 403/404 must surface as a clear, non-leaking message
        raise DriveDestinationError(
            "This Google Drive folder is not reachable with the connected Google account. "
            "Reconize can only write into folders it created itself — paste a folder Reconize "
            "created, or reconnect Google Drive to grant access to a new one."
        ) from e

    if meta.get("trashed"):
        raise DriveDestinationError("That Google Drive folder is in the trash.")
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        raise DriveDestinationError("That link points to a file, not a folder.")
    capabilities = meta.get("capabilities") or {}
    if capabilities.get("canAddChildren") is False:
        raise DriveDestinationError("The connected Google account does not have upload permission in that folder.")

    return {"folder_id": folder_id, "folder_name": meta.get("name") or folder_id, "can_upload": True}


_upload_lock = threading.Lock()
_uploading_batches: set[str] = set()


def reserve_upload(batch_id: str) -> bool:
    """Atomically claims the upload slot for a batch. Returns False if an
    upload for this batch is already running — the caller must treat that as
    a rejected duplicate start, not a queued retry."""
    with _upload_lock:
        if batch_id in _uploading_batches:
            return False
        _uploading_batches.add(batch_id)
        return True


def _release_upload(batch_id: str) -> None:
    with _upload_lock:
        _uploading_batches.discard(batch_id)


@dataclass
class _UploadFile:
    category: str  # "MEDIA" or a person's SORTED folder name
    path: Path


def _iter_upload_files(root: Path, upload_people: bool, upload_media: bool) -> list[_UploadFile]:
    """Only ever walks SORTED/ and MEDIA/ under the batch's local output
    root. REVIEW, ORIGINAL, LOGO, the database and any other local file are
    never listed here, let alone uploaded."""
    files: list[_UploadFile] = []
    if upload_media:
        media_dir = root / "MEDIA"
        if media_dir.is_dir():
            for p in sorted(media_dir.iterdir()):
                if p.is_file():
                    files.append(_UploadFile("MEDIA", _safe_path(root, p)))
    if upload_people:
        sorted_dir = root / "SORTED"
        if sorted_dir.is_dir():
            for person_dir in sorted(p for p in sorted_dir.iterdir() if p.is_dir()):
                _safe_path(root, person_dir)
                for p in sorted(person_dir.iterdir()):
                    if p.is_file():
                        files.append(_UploadFile(person_dir.name, _safe_path(root, p)))
    return files


def run_drive_upload(batch_id: str, folder_url: str, upload_people: bool, upload_media: bool) -> None:
    """Background-task entry point. Always releases the concurrency slot. Any
    DriveDestinationError raised inside _run_upload has already been recorded
    on the batch (status=upload_failed, drive_error set) before it propagates
    here, so it is only logged, never re-raised further."""
    try:
        _run_upload(batch_id, folder_url, upload_people, upload_media)
    except DriveDestinationError as e:
        logger.warning("Photo batch %s: Drive destination upload failed: %s", batch_id, e)
    except Exception:  # noqa: BLE001 — never strand the concurrency slot
        logger.exception("Photo batch %s: Drive destination upload raised unexpectedly", batch_id)
    finally:
        _release_upload(batch_id)


def _fail(batch_id: str, message: str) -> None:
    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if batch:
            batch.status = "upload_failed"
            batch.drive_error = message
            session.add(batch)
            session.commit()


def _run_upload(batch_id: str, folder_url: str, upload_people: bool, upload_media: bool) -> None:
    folder_id = parse_destination_folder_url(folder_url)

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            raise DriveDestinationError("Photo batch not found.")
        if not local_output_ready(batch, STORAGE_PATH):
            raise DriveDestinationError("Local processing output is not ready — finish processing first.")
        root = batch_output_root(batch, STORAGE_PATH)
        files = _iter_upload_files(root, upload_people, upload_media)
        if not files:
            raise DriveDestinationError("Nothing to upload — no local Person/SORTED or MEDIA files were found.")

        label = batch.label
        batch.status = "uploading"
        batch.drive_error = None
        batch.current_stage = f"Uploading to Google Drive — 0 of {len(files)}"
        session.add(batch)
        session.commit()

    try:
        run_root_id = create_subfolder(folder_id, f"Reconize — {label} — {datetime.now():%Y-%m-%d %H%M%S}")
        subfolder_ids: dict[str, str] = {}
        media_folder_id = ""

        for index, item in enumerate(files, start=1):
            if item.category == "MEDIA":
                if not media_folder_id:
                    media_folder_id = get_or_create_subfolder(run_root_id, "MEDIA")
                target_id = media_folder_id
            else:
                target_id = subfolder_ids.get(item.category)
                if not target_id:
                    target_id = get_or_create_subfolder(run_root_id, item.category)
                    subfolder_ids[item.category] = target_id

            mime_type = mimetypes.guess_type(item.path.name)[0] or "image/jpeg"
            data = item.path.read_bytes()
            upload_bytes(target_id, item.path.name, data, mime_type)

            with Session(engine) as session:
                batch = session.get(PhotoBatch, batch_id)
                if not batch:
                    return
                batch.current_stage = f"Uploading to Google Drive — {index} of {len(files)}: {item.path.name}"
                session.add(batch)
                session.commit()
    except Exception as e:  # noqa: BLE001 — local output must survive an upload failure
        _fail(batch_id, str(e))
        raise DriveDestinationError(str(e)) from e

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if batch:
            batch.status = "completed"
            batch.current_stage = ""
            batch.processed_folder_id = run_root_id
            batch.media_folder_id = media_folder_id
            session.add(batch)
            session.commit()
