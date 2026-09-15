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

import hashlib
import io
import json
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlmodel import Session

from app.config import STORAGE_PATH
from app.database.db import engine
from app.models.models import PhotoBatch
from app.services.google_drive_oauth_service import (
    DriveOAuthError,
    get_write_service,
)
from app.services.photo_batch_download_service import _safe_path, batch_output_root, local_output_ready

import logging

logger = logging.getLogger(__name__)


class DriveDestinationError(Exception):
    pass


# ---- Actionable errors ------------------------------------------------------
# The destination grant is drive.file: Reconize can write only into folders the
# user selected with the Google Picker (any owner — My Drive, shared with me,
# or a Shared Drive — as long as the account can add files). A folder that was
# never selected returns 404. Reconnecting never grants a folder.

NOT_GRANTED_MESSAGE = "Reconize cannot access this folder yet. Select it with Google Picker first."
EXPIRED_MESSAGE = (
    "The Google Drive connection has expired or was revoked. Click \"Connect Google Drive\" on the Event "
    "Photos page to reconnect, then try again."
)
NOT_CONNECTED_MESSAGE = (
    "My Google Drive is not connected. Click \"Connect Google Drive\" on the Event Photos page, then try again."
)
VIEW_ONLY_MESSAGE = (
    "The connected Google account can view this folder but cannot add files to it. Select a folder you can "
    "edit with Google Picker."
)

class PickerGrantRequired(DriveDestinationError):
    """The folder exists for the user, but Reconize has never been granted it
    under drive.file. This is NOT an OAuth problem and reconnecting does not
    fix it: Google requires one Picker confirmation for that folder, after
    which pasting its link keeps working."""

    def __init__(self, folder_id: str, message: str = NOT_GRANTED_MESSAGE):
        super().__init__(message)
        self.folder_id = folder_id


PICKER_GRANT_PROMPT = "Google requires one confirmation before Reconize can upload to this folder."

_SECRET_RE = re.compile(
    r"(?:access_token|refresh_token|client_secret|id_token|key)=[^&\s\"']+|Bearer\s+[\w.\-~+/]+|ya29\.[\w.\-]+|1//[\w.\-]+",
    re.IGNORECASE,
)


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text or "")


def _http_status(error: BaseException | None) -> int:
    """The Google HTTP status anywhere in the exception chain (0 if none)."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        resp = getattr(error, "resp", None)
        status = getattr(resp, "status", None) or getattr(error, "status_code", None)
        try:
            if status:
                return int(status)
        except (TypeError, ValueError):
            pass
        error = error.__cause__ or error.__context__
    return 0


def explain_drive_error(error: BaseException) -> str:
    """One specific, actionable sentence for the admin. Never contains a token."""
    text = _redact(str(error))
    lower = text.lower()
    status = _http_status(error)
    if isinstance(error, DriveOAuthError) and not status:
        if "not connected" in lower:
            return NOT_CONNECTED_MESSAGE
        if "not configured" in lower:
            return text
        if any(w in lower for w in ("refresh", "invalid_grant", "expired", "revoked")):
            return EXPIRED_MESSAGE
    if status == 404 or (not status and ("404" in lower or "not found" in lower)):
        return NOT_GRANTED_MESSAGE
    if status == 401:
        return EXPIRED_MESSAGE
    if status == 403:
        squashed = lower.replace(" ", "")
        if "storagequota" in squashed or ("quota" in lower and "storage" in lower):
            return "The connected Google Drive is full (storage quota exceeded). Free up space in that Google account, then retry."
        if "ratelimit" in squashed or "rate limit" in lower:
            return "Google Drive is temporarily limiting requests. Wait a few minutes, then retry the upload."
        return ("The connected Google account does not have permission to add files to that folder. "
                "Select a folder you can edit with Google Picker.")
    if status >= 500:
        return f"Google Drive had a temporary problem (HTTP {status}). Retry the upload in a moment."
    if isinstance(error, (TimeoutError, ConnectionError)) or any(
            w in lower for w in ("timed out", "timeout", "name resolution", "unreachable", "connection reset")):
        return "Could not reach Google Drive. Check this computer's internet connection, then retry."
    return f"Google Drive refused the request: {text[:300]}"


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


def resolve_folder_ref(folder_url: str | None = None, folder_id: str | None = None) -> str:
    """Phase H — a destination arrives either as a pasted URL or as the id a
    Google Picker returned. Both end in the same strict id check and then in
    validate_destination(): a Picker selection is never trusted on its own."""
    if folder_id:
        candidate = folder_id.strip()
        if not _RAW_ID_RE.match(candidate):
            raise DriveDestinationError("That is not a valid Google Drive folder id.")
        return candidate
    return parse_destination_folder_url(folder_url or "")


_FOLDER_MIME = "application/vnd.google-apps.folder"
_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_META_FIELDS = "id,name,mimeType,trashed,driveId,capabilities(canAddChildren),shortcutDetails(targetId,targetMimeType)"


def _drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _get_meta(service, file_id: str) -> dict:
    try:
        # supportsAllDrives: Shared Drive folders are valid destinations.
        return service.files().get(fileId=file_id, fields=_META_FIELDS, supportsAllDrives=True).execute()
    except Exception as e:  # noqa: BLE001 — Drive 403/404 must surface as a clear, non-leaking message
        message = explain_drive_error(e)
        if message == NOT_GRANTED_MESSAGE:
            # Recoverable by one Picker confirmation — the caller keeps the id.
            raise PickerGrantRequired(file_id, message) from e
        raise DriveDestinationError(message) from e


def validate_destination(folder_id: str) -> dict:
    """Uses the EXISTING authenticated Drive write client — never expands
    scope. Confirms the folder exists, is really a folder (not trashed, not
    a file), and that the connected account can add files to it — whoever
    owns it. A shortcut is followed once, and only to a folder that passes
    the same checks. Raises DriveDestinationError with a clear message
    otherwise; never returns tokens or credentials."""
    try:
        service = get_write_service()
    except DriveOAuthError as e:
        raise DriveDestinationError(explain_drive_error(e)) from e

    meta = _get_meta(service, folder_id)
    if meta.get("mimeType") == _SHORTCUT_MIME:
        target = meta.get("shortcutDetails") or {}
        target_id = str(target.get("targetId") or "")
        if meta.get("trashed"):
            raise DriveDestinationError("That Google Drive shortcut is in the trash.")
        if target.get("targetMimeType") != _FOLDER_MIME or not _RAW_ID_RE.match(target_id):
            raise DriveDestinationError("That shortcut does not point to a folder. Select the folder itself with Google Picker.")
        folder_id, meta = target_id, _get_meta(service, target_id)
        if meta.get("mimeType") == _SHORTCUT_MIME:
            raise DriveDestinationError("That shortcut points to another shortcut. Select the folder itself with Google Picker.")

    if meta.get("trashed"):
        raise DriveDestinationError("That Google Drive folder is in the trash.")
    if meta.get("mimeType") != _FOLDER_MIME:
        raise DriveDestinationError("That link points to a file, not a folder.")
    capabilities = meta.get("capabilities") or {}
    # Require an explicit True: missing permission evidence refuses.
    if capabilities.get("canAddChildren") is not True:
        raise DriveDestinationError(VIEW_ONLY_MESSAGE)

    return {"folder_id": folder_id, "folder_name": meta.get("name") or folder_id, "can_upload": True,
            "folder_url": _drive_folder_url(folder_id)}


# ---- Which Google account receives the upload -------------------------------
# The DESTINATION OAuth account, read from Drive's own about.get under the
# existing drive.file scope — never the SOURCE service account, and never a
# token, refresh token, client secret or storage path.

_ACCOUNT_TTL_SECONDS = 60
_account_lock = threading.Lock()
_account_cache: tuple[float, dict] | None = None


def invalidate_connected_account() -> None:
    """After a connect, reconnect or disconnect: a previous account must never
    be shown as the current one."""
    global _account_cache
    with _account_lock:
        _account_cache = None


def _account_failure_reason(error: BaseException) -> str:
    status = _http_status(error)
    text = str(error).lower()
    if isinstance(error, DriveOAuthError) and "not connected" in text:
        return "not_connected"
    if status in (401, 403) or any(w in text for w in ("invalid_grant", "expired", "revoked", "reconnect")):
        return "reauthorization_required"
    return "verification_unavailable"


def connected_account(force_refresh: bool = False) -> dict:
    """Identity of the account uploads will run as: display name, email, photo
    and Drive's own stable permission id. Cached briefly so opening a page does
    not hit Google on every render; never called from progress polling."""
    global _account_cache
    now = time.monotonic()
    with _account_lock:
        cached = _account_cache
    if not force_refresh and cached and now - cached[0] < _ACCOUNT_TTL_SECONDS:
        return dict(cached[1])

    try:
        service = get_write_service()
        about = service.about().get(fields="user(displayName,emailAddress,photoLink,permissionId)").execute()
    except Exception as e:  # noqa: BLE001 — identity lookup never breaks the page
        reason = _account_failure_reason(e)
        logger.info("Could not verify the connected Google Drive account (%s)", reason)
        with _account_lock:
            _account_cache = None
        return {"connected": False, "account": None, "reason": reason}

    user = (about or {}).get("user") or {}
    email = user.get("emailAddress") or ""
    if not email:
        return {"connected": False, "account": None, "reason": "verification_unavailable"}
    result = {
        "connected": True,
        "reason": None,
        "account": {
            "display_name": user.get("displayName") or "",
            "email": email,
            "photo_url": user.get("photoLink") or None,
            # Drive's own permission id: stable, non-secret, and never a token.
            "account_id": user.get("permissionId") or email,
        },
    }
    with _account_lock:
        _account_cache = (now, result)
    return dict(result)


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


# ---- Idempotent uploads and retries -----------------------------------------
# A Retry must neither duplicate a file nor skip one by name: two different
# outputs legitimately share a filename (MEDIA/p1.jpg and SORTED/<person>/p1.jpg).
# Every uploaded file therefore carries a STABLE KEY derived from the batch id
# and the output-relative path, written to the Drive file's appProperties and
# mirrored in a small local manifest. A retry trusts a stored file id only after
# Drive confirms the file still sits in the expected folder and is not trashed;
# otherwise it asks Drive for that exact key (which is what recovers an upload
# that succeeded at Google but whose answer never came back).

_UPLOAD_KEY_PROP = "reconizeUploadKey"
_BATCH_PROP = "reconizeBatchId"
# Kept beside the outputs, never exported: ZIP and Drive only walk
# MEDIA/SORTED/AMBIENCE, and it is deleted with the batch.
_STATE_DIRNAME = "DRIVE_UPLOAD"


def upload_key(batch_id: str, rel_path: str) -> str:
    return hashlib.sha256(f"{batch_id}|{rel_path}".encode()).hexdigest()


def _state_path(root: Path) -> Path:
    return root / _STATE_DIRNAME / "state.json"


def _load_state(root: Path, folder_id: str) -> dict:
    """What this batch already put in THIS destination. A different destination
    starts empty — ids from a previous folder are never reused."""
    try:
        state = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("destination") != folder_id:
        return {"destination": folder_id, "folders": {}, "files": {}}
    state.setdefault("folders", {})
    state.setdefault("files", {})
    return state


def _save_state(root: Path, state: dict) -> None:
    """Written after every single file, so an interrupted run resumes exactly."""
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("state.json.writing")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\'")


def _find_by_key(service, parent_id: str, key: str, folder: bool = False) -> str | None:
    """The one item in this exact parent carrying this stable key. Two of them
    is a conflict this code reports instead of making worse."""
    query = (f"'{parent_id}' in parents and trashed = false and "
             f"appProperties has {{ key='{_UPLOAD_KEY_PROP}' and value='{key}' }}")
    if folder:
        query += f" and mimeType = '{_FOLDER_MIME}'"
    try:
        resp = (service.files()
                .list(q=query, fields="files(id,name)", pageSize=10, supportsAllDrives=True,
                      includeItemsFromAllDrives=True)
                .execute())
    except Exception as e:  # noqa: BLE001 — surfaces as a clear, non-leaking message
        raise DriveDestinationError(explain_drive_error(e)) from e
    found = resp.get("files") or []
    if len(found) > 1:
        raise DriveDestinationError(
            f"Google Drive already holds {len(found)} copies of the same Reconize output in the destination folder. "
            "Remove the duplicates in Drive, then retry — Reconize will not add another copy.")
    return found[0]["id"] if found else None


def _still_in_place(service, file_id: str, parent_id: str) -> bool:
    """A stored Drive file id is trusted only while Drive still says the file
    is in the expected folder and not trashed."""
    try:
        meta = service.files().get(fileId=file_id, fields="id,trashed,parents", supportsAllDrives=True).execute()
    except Exception as e:  # noqa: BLE001
        if _http_status(e) == 404:
            return False
        raise DriveDestinationError(explain_drive_error(e)) from e
    return not meta.get("trashed") and parent_id in (meta.get("parents") or [])


def _ensure_subfolder(service, batch_id: str, parent_id: str, name: str, key: str) -> str:
    """The MEDIA / person / AMBIENCE subfolder, found by its stable key first —
    never by name alone. An existing folder of that name is adopted once and
    stamped with the key, so later runs find exactly it."""
    existing = _find_by_key(service, parent_id, key, folder=True)
    if existing:
        return existing
    props = {_UPLOAD_KEY_PROP: key, _BATCH_PROP: batch_id}
    query = (f"'{parent_id}' in parents and trashed = false and mimeType = '{_FOLDER_MIME}' "
             f"and name = '{_escape_query_value(name)}'")
    try:
        resp = (service.files()
                .list(q=query, fields="files(id)", pageSize=2, supportsAllDrives=True, includeItemsFromAllDrives=True)
                .execute())
        matches = resp.get("files") or []
        if matches:
            folder_id = matches[0]["id"]
            service.files().update(fileId=folder_id, body={"appProperties": props}, supportsAllDrives=True).execute()
            return folder_id
        created = (service.files()
                   .create(body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id], "appProperties": props},
                           fields="id", supportsAllDrives=True)
                   .execute())
    except DriveDestinationError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DriveDestinationError(explain_drive_error(e)) from e
    return created["id"]


def _upload_file(service, parent_id: str, name: str, data: bytes, mime_type: str, props: dict) -> str:
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    created = (service.files()
               .create(body={"name": name, "parents": [parent_id], "appProperties": props},
                       media_body=media, fields="id", supportsAllDrives=True)
               .execute())
    return created["id"]


@dataclass
class _UploadFile:
    category: str  # "MEDIA" or a person's SORTED folder name
    path: Path


def _iter_upload_files(root: Path, upload_people: bool, upload_media: bool, upload_ambience: bool = False,
                       sorted_folders: set[str] | None = None) -> list[_UploadFile]:
    """Only ever walks MEDIA/, SORTED/ and (Phase O) AMBIENCE/ under the
    batch's local output root, and every file must pass the SAME eligibility
    rule the ZIP uses (export_selection_service.eligible). REVIEW, ORIGINAL,
    THUMBNAILS, LOGO, the database and any other local file are never listed
    here, let alone uploaded."""
    from app.services.export_selection_service import ExportSelection, eligible

    selection = ExportSelection(media=upload_media, sorted=upload_people, ambience=upload_ambience)
    files: list[_UploadFile] = []
    if upload_media:
        media_dir = root / "MEDIA"
        if media_dir.is_dir():
            for p in sorted(media_dir.iterdir()):
                if p.is_file() and eligible(p.relative_to(root).parts, selection, sorted_folders):
                    files.append(_UploadFile("MEDIA", _safe_path(root, p)))
    if upload_people:
        sorted_dir = root / "SORTED"
        if sorted_dir.is_dir():
            for person_dir in sorted(p for p in sorted_dir.iterdir() if p.is_dir()):
                _safe_path(root, person_dir)
                for p in sorted(person_dir.iterdir()):
                    if p.is_file() and eligible(p.relative_to(root).parts, selection, sorted_folders):
                        files.append(_UploadFile(person_dir.name, _safe_path(root, p)))
    if upload_ambience:
        ambience_dir = root / "AMBIENCE"
        if ambience_dir.is_dir():
            for p in sorted(ambience_dir.iterdir()):
                if p.is_file() and eligible(p.relative_to(root).parts, selection, sorted_folders):
                    files.append(_UploadFile("AMBIENCE", _safe_path(root, p)))
    return files


def run_drive_upload(batch_id: str, folder_url: str, upload_people: bool, upload_media: bool,
                     upload_ambience: bool = False, sorted_folders: set[str] | None = None) -> None:
    """Background-task entry point. Always releases the concurrency slot. Any
    DriveDestinationError raised inside _run_upload has already been recorded
    on the batch (status=upload_failed, drive_error set) before it propagates
    here, so it is only logged, never re-raised further."""
    try:
        _run_upload(batch_id, folder_url, upload_people, upload_media, upload_ambience, sorted_folders)
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
            if batch.workflow_version >= 2:
                batch.drive_status = "UPLOAD_FAILED"  # Phase M2 — suspends retention deletion, same as the legacy status already does
            batch.drive_error = message
            session.add(batch)
            session.commit()


def _run_upload(batch_id: str, folder_url: str, upload_people: bool, upload_media: bool,
                upload_ambience: bool = False, sorted_folders: set[str] | None = None) -> None:
    folder_id = parse_destination_folder_url(folder_url)

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            raise DriveDestinationError("Photo batch not found.")
        if not local_output_ready(batch, STORAGE_PATH):
            raise DriveDestinationError("Local processing output is not ready — finish processing first.")
        root = batch_output_root(batch, STORAGE_PATH)
        files = _iter_upload_files(root, upload_people, upload_media, upload_ambience, sorted_folders)
        if not files:
            raise DriveDestinationError("Nothing to upload — no local Person/SORTED or MEDIA files were found.")

    # Revalidated right before uploading: access or edit permission may have
    # been removed since the folder was selected. Fails closed.
    try:
        folder_id = validate_destination(folder_id)["folder_id"]
    except DriveDestinationError as e:
        _fail(batch_id, str(e))
        raise

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if not batch:
            raise DriveDestinationError("Photo batch not found.")
        batch.status = "uploading"
        if batch.workflow_version >= 2:
            batch.drive_status = "UPLOADING"  # Phase M2 — never deleted by retention while this holds
        batch.drive_error = None
        batch.current_stage = f"Uploading to Google Drive — 0 of {len(files)}"
        session.add(batch)
        session.commit()

    try:
        # Directly inside the selected folder — no extra top-level run folder.
        # MEDIA / per-person / AMBIENCE subfolders are created (or reused) there.
        service = get_write_service()
        state = _load_state(root, folder_id)
        subfolder_ids: dict[str, str] = {}
        reused = 0

        for index, item in enumerate(files, start=1):
            rel = item.path.relative_to(root).as_posix()
            key = upload_key(batch_id, rel)

            target_id = subfolder_ids.get(item.category)
            if not target_id:
                folder_key = upload_key(batch_id, f"folder:{item.category}")
                recorded = state["folders"].get(item.category)
                if recorded and _still_in_place(service, recorded, folder_id):
                    target_id = recorded
                else:
                    target_id = _ensure_subfolder(service, batch_id, folder_id, item.category, folder_key)
                subfolder_ids[item.category] = target_id
                state["folders"][item.category] = target_id
                _save_state(root, state)

            # 1. a recorded id Drive still confirms, 2. otherwise this exact key
            # in this exact folder (recovers a success whose answer was lost),
            # 3. otherwise upload. Never a match on filename.
            recorded_file = state["files"].get(key) or {}
            file_id = None
            if recorded_file.get("parent") == target_id and recorded_file.get("file_id"):
                if _still_in_place(service, recorded_file["file_id"], target_id):
                    file_id = recorded_file["file_id"]
            if file_id is None:
                file_id = _find_by_key(service, target_id, key)
            if file_id is None:
                mime_type = mimetypes.guess_type(item.path.name)[0] or "image/jpeg"
                file_id = _upload_file(service, target_id, item.path.name, item.path.read_bytes(), mime_type,
                                       {_UPLOAD_KEY_PROP: key, _BATCH_PROP: batch_id})
            else:
                reused += 1
            state["files"][key] = {"file_id": file_id, "parent": target_id, "path": rel}
            _save_state(root, state)

            with Session(engine) as session:
                batch = session.get(PhotoBatch, batch_id)
                if not batch:
                    return
                batch.current_stage = f"Uploading to Google Drive — {index} of {len(files)}: {item.path.name}"
                session.add(batch)
                session.commit()
        if reused:
            logger.info("Photo batch %s: %d file(s) were already in the destination and were not uploaded again",
                        batch_id, reused)
    except DriveDestinationError as e:
        _fail(batch_id, str(e))
        raise
    except Exception as e:  # noqa: BLE001 — local output must survive an upload failure
        message = explain_drive_error(e)
        _fail(batch_id, message)
        raise DriveDestinationError(message) from e

    with Session(engine) as session:
        batch = session.get(PhotoBatch, batch_id)
        if batch:
            batch.status = "completed"
            if batch.workflow_version >= 2:
                batch.drive_status = "UPLOADED"  # Phase M2 — original expiry continues, never restarted
            batch.current_stage = ""
            batch.processed_folder_id = folder_id
            batch.media_folder_id = subfolder_ids.get("MEDIA", state["folders"].get("MEDIA", ""))
            session.add(batch)
            session.commit()
