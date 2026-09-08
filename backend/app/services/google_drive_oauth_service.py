"""Admin-side Google Drive OAuth — used only for the WRITE half of Event
Photo Processing (creating the PROCESSED folder tree, uploading originals
and MEDIA copies, copying between folders).

Why this exists separately from google_drive_folder_service.py's service
account: a bare Google service account has no personal Drive storage quota
on a normal consumer ("My Drive") account. Creating folders costs no quota
and works fine under the service account; uploading real file bytes does
cost quota, which the service account doesn't have — every such upload
fails. A real Google account (the admin's own, via a one-time OAuth login)
always has real quota, so writes run under that identity instead. Reads
(listing/downloading the photographer's photos) stay on the service account
— that already works and needs no quota.

Hand-rolled against the token endpoint directly with `requests` (already a
dependency) rather than adding google-auth-oauthlib.
"""
from __future__ import annotations

import io
import secrets
from urllib.parse import urlencode

import requests
from sqlmodel import Session

from app.config import GOOGLE_DRIVE_CLIENT_ID, GOOGLE_DRIVE_CLIENT_SECRET, OAUTH_REDIRECT_URI
from app.database.db import engine
from app.models.models import Setting

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# drive.file, NOT the full drive scope. drive.file is a non-restricted scope,
# so this app can be published to Production without Google's paid annual
# security assessment (which the full drive scope requires) — meaning any
# admin can connect, not just accounts on a Testing-mode allowlist, and the
# refresh token does not expire every 7 days.
#
# The trade-off, and the reason the output tree lives where it does: drive.file
# grants access ONLY to files and folders this app itself created. It cannot
# write into the photographer's existing folder, so the PROCESSED tree is
# created in the connected admin's own Drive instead. Reading the
# photographer's photos is unaffected — that runs on the service account
# (google_drive_folder_service.py), which needs no user consent at all.
SCOPES = "https://www.googleapis.com/auth/drive.file"

_REFRESH_TOKEN_KEY = "google_drive_oauth_refresh_token"
_EMAIL_KEY = "google_drive_oauth_email"
_PENDING_STATE_KEY = "google_drive_oauth_pending_state"


class DriveOAuthError(Exception):
    pass


def _get_setting(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        row = Setting(key=key, value=value)
    session.add(row)
    session.commit()


def generate_pending_state() -> str:
    """Single-admin app, so one pending state at a time is enough — /start
    writes it, /callback consumes (and clears) it. Guards the callback
    (which can't carry a JWT, since Google redirects the bare browser to it)
    against a forged/replayed request."""
    state = secrets.token_urlsafe(24)
    with Session(engine) as session:
        _set_setting(session, _PENDING_STATE_KEY, state)
    return state


def verify_and_clear_pending_state(state: str) -> bool:
    with Session(engine) as session:
        expected = _get_setting(session, _PENDING_STATE_KEY)
        _set_setting(session, _PENDING_STATE_KEY, "")
    return bool(expected) and secrets.compare_digest(expected, state)


def get_auth_url(state: str) -> str:
    if not GOOGLE_DRIVE_CLIENT_ID:
        raise DriveOAuthError("GOOGLE_DRIVE_CLIENT_ID is not set in .env — create an OAuth client in Google Cloud Console first.")
    params = {
        "client_id": GOOGLE_DRIVE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_and_store(code: str) -> str:
    """Exchanges an auth code for tokens, stores the refresh token + the
    connected account's email, and returns that email."""
    if not (GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET):
        raise DriveOAuthError("Google Drive OAuth client is not configured — set GOOGLE_DRIVE_CLIENT_ID/SECRET in .env.")

    resp = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": GOOGLE_DRIVE_CLIENT_ID,
        "client_secret": GOOGLE_DRIVE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=20)
    if resp.status_code != 200:
        raise DriveOAuthError(f"Google rejected the OAuth code exchange: {resp.text}")
    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not refresh_token:
        raise DriveOAuthError(
            "Google did not return a refresh token. This usually means the account already has a prior "
            "grant — revoke this app's access at https://myaccount.google.com/permissions and try again."
        )

    email = ""
    try:
        info = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        if info.status_code == 200:
            email = info.json().get("email", "")
    except requests.RequestException:
        pass

    with Session(engine) as session:
        _set_setting(session, _REFRESH_TOKEN_KEY, refresh_token)
        _set_setting(session, _EMAIL_KEY, email)
    return email


def is_connected() -> bool:
    with Session(engine) as session:
        return bool(_get_setting(session, _REFRESH_TOKEN_KEY))


def connected_email() -> str | None:
    with Session(engine) as session:
        return _get_setting(session, _EMAIL_KEY)


def _get_write_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    with Session(engine) as session:
        refresh_token = _get_setting(session, _REFRESH_TOKEN_KEY)
    if not refresh_token:
        raise DriveOAuthError("Google Drive is not connected — click \"Connect Google Drive\" on the Event Photos page first.")
    if not (GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET):
        raise DriveOAuthError("Google Drive OAuth client is not configured — set GOOGLE_DRIVE_CLIENT_ID/SECRET in .env.")

    creds = Credentials(
        None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URL,
        client_id=GOOGLE_DRIVE_CLIENT_ID,
        client_secret=GOOGLE_DRIVE_CLIENT_SECRET,
        scopes=[SCOPES],
    )
    try:
        creds.refresh(Request())
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not refresh the Google Drive connection — reconnect it: {e}") from e
    return creds


def get_write_service():
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_get_write_credentials(), cache_discovery=False)


def create_root_folder(name: str) -> str:
    """Creates the batch's top-level output folder in the connected admin's
    own Drive and returns its id.

    Under drive.file this app can only touch what it created, so every output
    folder must descend from a folder created here — it cannot be placed
    inside the photographer's existing folder. Not idempotent by name on
    purpose: two runs of the same source folder produce two separate,
    clearly-dated output folders rather than silently merging into one."""
    service = get_write_service()
    try:
        folder = (
            service.files()
            .create(body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id")
            .execute()
        )
        return folder["id"]
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not create the Drive output folder '{name}': {e}") from e


def create_subfolder(parent_id: str, name: str) -> str:
    """Like create_root_folder but nested under an existing parent_id instead
    of the Drive root — used for mirroring into a user-pasted destination
    folder. Not idempotent by name, same reasoning as create_root_folder:
    each upload run gets its own clearly-dated folder rather than merging
    into a previous run's."""
    service = get_write_service()
    try:
        folder = (
            service.files()
            .create(body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}, fields="id")
            .execute()
        )
        return folder["id"]
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not create the Drive output folder '{name}': {e}") from e


def get_or_create_subfolder(parent_id: str, name: str) -> str:
    """Idempotent: returns the existing child folder's id if one with this
    name is already there, otherwise creates it. Avoids duplicate PROCESSED/
    AMBIENCE/REVIEW/MEDIA/participant folders if a batch is ever retried."""
    service = get_write_service()
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"'{parent_id}' in parents and name = '{safe_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    try:
        resp = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
        existing = resp.get("files", [])
        if existing:
            return existing[0]["id"]
        folder = (
            service.files()
            .create(
                body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
                fields="id",
            )
            .execute()
        )
        return folder["id"]
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not create Drive folder '{name}': {e}") from e


def verify_file(file_id: str, expected_parent_id: str, expect_nonempty: bool = True) -> None:
    """Confirms an upload/copy actually landed: the id resolves, the file is
    not trashed, it really sits in the folder we targeted, and it has real
    bytes. Raises DriveOAuthError if any of that fails.

    This exists because Drive can return a plausible-looking id for a write
    that did not truly succeed — the earlier service-account quota failure
    created folders fine but produced no files, and nothing noticed. Never
    report an upload as successful without this check."""
    service = get_write_service()
    try:
        meta = service.files().get(fileId=file_id, fields="id,parents,size,trashed").execute()
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Upload verification failed — Drive file {file_id} could not be read back: {e}") from e

    if meta.get("trashed"):
        raise DriveOAuthError(f"Upload verification failed — Drive file {file_id} is in the trash.")
    parents = meta.get("parents") or []
    if expected_parent_id not in parents:
        raise DriveOAuthError(
            f"Upload verification failed — Drive file {file_id} is in {parents or 'no folder'}, expected {expected_parent_id}."
        )
    if expect_nonempty and int(meta.get("size") or 0) <= 0:
        raise DriveOAuthError(f"Upload verification failed — Drive file {file_id} is empty (0 bytes).")


def upload_bytes(parent_id: str, filename: str, data: bytes, mime_type: str) -> str:
    """Uploads raw bytes as a new file inside parent_id, verifies it actually
    arrived there, and returns the verified file id."""
    from googleapiclient.http import MediaIoBaseUpload

    service = get_write_service()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    try:
        f = service.files().create(body={"name": filename, "parents": [parent_id]}, media_body=media, fields="id").execute()
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not upload '{filename}' to Drive: {e}") from e

    file_id = f.get("id")
    if not file_id:
        raise DriveOAuthError(f"Drive did not return a file id for '{filename}'.")
    verify_file(file_id, parent_id)
    return file_id


def copy_file(file_id: str, parent_id: str, name: str | None = None) -> str:
    """Server-side copy of an existing Drive file into another folder — no
    bytes travel through this app, so placing the same unmodified original in
    several participant folders never re-uploads it.

    Note: file_id here is a file the ADMIN's OAuth identity just uploaded
    (not a service-account-owned file), so the admin always has copy rights
    on it."""
    service = get_write_service()
    body: dict = {"parents": [parent_id]}
    if name:
        body["name"] = name
    try:
        f = service.files().copy(fileId=file_id, body=body, fields="id").execute()
    except Exception as e:  # noqa: BLE001
        raise DriveOAuthError(f"Could not copy file in Drive: {e}") from e

    new_id = f.get("id")
    if not new_id:
        raise DriveOAuthError("Drive did not return a file id for the copy.")
    verify_file(new_id, parent_id)
    return new_id
