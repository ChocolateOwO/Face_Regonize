"""Service-account access to a shared Google Drive FOLDER — listing and
downloading every image inside it. Read-only: this service account has no
personal Drive storage quota, so it cannot upload/create files with content
(see google_drive_oauth_service.py for the write path, which runs under the
admin's own OAuth-connected Google account instead).

This is deliberately separate from google_drive_service.py, which only ever
downloads a single publicly-shared file by URL and needs no credentials at
all. Listing the contents of a folder is a different Drive API capability
(files.list) that Google requires real authentication for, even when the
folder is shared as "anyone with the link" — a plain API key is not enough
for general folder listing.

Setup (one-time, done by the admin outside this app): create a Google Cloud
service account, download its JSON key, set GOOGLE_SERVICE_ACCOUNT_KEY_PATH
in .env to point at it, then have the photographer share their Drive folder
with that service account's email address (found inside the key file) —
Viewer access is enough, since this service only ever reads.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from app.config import GOOGLE_SERVICE_ACCOUNT_KEY_PATH

_FOLDER_ID_IN_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_RAW_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveFolderError(Exception):
    pass


@dataclass
class DriveImageFile:
    id: str
    name: str
    mime_type: str


def extract_folder_id(url_or_id: str) -> str:
    """Accepts either a full "https://drive.google.com/drive/folders/ID"
    share link or a bare folder ID typed directly."""
    value = url_or_id.strip()
    match = _FOLDER_ID_IN_URL_RE.search(value)
    if match:
        return match.group(1)
    if _RAW_ID_RE.match(value):
        return value
    raise DriveFolderError(f"Could not parse a Google Drive folder ID from: {url_or_id}")


def _get_service():
    if not GOOGLE_SERVICE_ACCOUNT_KEY_PATH:
        raise DriveFolderError(
            "Google Drive is not configured — set GOOGLE_SERVICE_ACCOUNT_KEY_PATH in .env "
            "to a service account JSON key file."
        )
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        raise DriveFolderError(f"Google Drive libraries are not installed: {e}") from e

    try:
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_KEY_PATH, scopes=SCOPES
        )
    except Exception as e:  # noqa: BLE001 — surface any key-file problem clearly
        raise DriveFolderError(f"Could not load the service account key file: {e}") from e

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def service_account_email() -> str:
    """The email the photographer needs to share their folder with — read
    straight from the key file so the admin doesn't have to dig for it."""
    import json

    if not GOOGLE_SERVICE_ACCOUNT_KEY_PATH:
        return ""
    try:
        with open(GOOGLE_SERVICE_ACCOUNT_KEY_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except Exception:  # noqa: BLE001
        return ""


def get_folder_name(folder_id: str) -> str:
    """The photographer's folder name, used to label the batch and its
    output folder. Returns "" if it can't be read — the caller falls back to
    the folder id rather than failing the batch over a cosmetic label."""
    try:
        meta = _get_service().files().get(fileId=folder_id, fields="name").execute()
        return meta.get("name", "")
    except Exception:  # noqa: BLE001
        return ""


def list_image_files(folder_id: str) -> list[DriveImageFile]:
    service = _get_service()
    files: list[DriveImageFile] = []
    page_token = None
    query = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false"
    try:
        while True:
            response = (
                service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )
            for f in response.get("files", []):
                files.append(DriveImageFile(id=f["id"], name=f["name"], mime_type=f["mimeType"]))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:  # noqa: BLE001 — surface Drive/auth failures clearly to the caller
        raise DriveFolderError(f"Could not list the Drive folder — check that it's shared with {service_account_email() or 'the service account'}: {e}") from e
    return files


def download_file(file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    service = _get_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def folder_link(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"
