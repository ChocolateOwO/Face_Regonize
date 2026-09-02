"""Parses Google Drive share URLs and downloads publicly-shared files.

Only handles files shared as "Anyone with the link -> Viewer". Private files
are explicitly rejected with a clear error rather than any attempt to bypass
permissions; supporting those would require the Google Drive OAuth flow
(client id/secret in .env), which is out of scope for this local MVP.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

DOWNLOAD_URL = "https://drive.google.com/uc?export=download"
_ID_IN_PATH_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)")
_CONFIRM_TOKEN_RE = re.compile(r'confirm=([0-9A-Za-z_-]+)')


class GoogleDriveError(Exception):
    pass


@dataclass
class DownloadedFile:
    content: bytes
    content_type: str


def is_google_drive_url(url: str) -> bool:
    return "drive.google.com" in url


def extract_file_id(url: str) -> str | None:
    match = _ID_IN_PATH_RE.search(url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "id" in qs and qs["id"]:
        return qs["id"][0]
    return None


def download_public_file(url: str, timeout: int = 20) -> DownloadedFile:
    file_id = extract_file_id(url)
    if not file_id:
        raise GoogleDriveError(f"Could not parse a Google Drive file ID from: {url}")

    session = requests.Session()
    try:
        response = session.get(DOWNLOAD_URL, params={"id": file_id}, stream=True, timeout=timeout)
    except requests.RequestException as e:
        raise GoogleDriveError(f"Network error contacting Google Drive: {e}") from e

    if response.status_code == 404:
        raise GoogleDriveError("File not found. It may have been deleted or the link is wrong.")
    if response.status_code != 200:
        raise GoogleDriveError(f"Google Drive returned HTTP {response.status_code}.")

    content_type = response.headers.get("Content-Type", "")

    # Large files / files needing a virus-scan bypass return an HTML confirm page
    # instead of the file directly. Extract the confirm token and retry once.
    if "text/html" in content_type:
        token = None
        for key, value in response.cookies.items():
            if key.startswith("download_warning"):
                token = value
                break
        if not token:
            match = _CONFIRM_TOKEN_RE.search(response.text)
            if match:
                token = match.group(1)

        if "sign in" in response.text.lower() or "permission" in response.text.lower() and not token:
            raise GoogleDriveError(
                "Unable to access Google Drive image. The file may be private or "
                "inaccessible. Please set sharing to 'Anyone with the link' (Viewer)."
            )
        if not token:
            raise GoogleDriveError(
                "Unable to access Google Drive image. The file may be private or "
                "inaccessible. Please update the sharing permission."
            )

        try:
            response = session.get(
                DOWNLOAD_URL, params={"id": file_id, "confirm": token}, stream=True, timeout=timeout
            )
        except requests.RequestException as e:
            raise GoogleDriveError(f"Network error contacting Google Drive: {e}") from e
        content_type = response.headers.get("Content-Type", "")

    content = response.content
    if not content or "text/html" in content_type:
        raise GoogleDriveError(
            "Unable to access Google Drive image. The file may be private or inaccessible."
        )

    return DownloadedFile(content=content, content_type=content_type)
