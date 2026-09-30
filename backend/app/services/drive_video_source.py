"""Link-only video input under drive.readonly. Never mutates remote files.

Only authenticated Drive metadata/media requests, never pasted download URLs.
Container extensions select candidates; the local decoder validates contents.
"""
from __future__ import annotations
import hashlib
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from googleapiclient.http import MediaIoBaseDownload
from app.services import google_drive_oauth_service as oauth
from app.services.drive_destination_service import parse_destination_folder_url, DriveDestinationError, _http_status
from app.services import local_video_experiment as video

MAX_FILES = 32
MAX_LIST_FILES = 1000
MAX_DURATION = 12 * 3600
# No per-file byte ceiling; downloads are bounded by the shared disk budget.
DISK_RESERVE = video.DISK_RESERVE
CHUNK_BYTES = 4 * 1024 * 1024
FIELDS = "id,name,mimeType,size,parents,trashed,driveId,resourceKey,modifiedTime,version,md5Checksum,capabilities(canDownload),videoMediaMetadata"
FOLDER_MIME = "application/vnd.google-apps.folder"

class DriveVideoError(ValueError):
    pass

class GrantRequired(DriveVideoError):
    pass

class DownloadCancelled(DriveVideoError):
    pass

def limits() -> dict:
    return dict(max_duration_seconds=MAX_DURATION, max_files=MAX_FILES,
        disk_reserve_bytes=DISK_RESERVE, max_pixels=video.MAX_PIXELS, max_fps=video.MAX_FPS,
        containers=sorted(video.EXTENSIONS))

def file_id(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{10,100}", value or ""):
        raise DriveVideoError("Invalid Google Drive file ID. List a valid Drive folder and select a direct child file.")
    return value

def folder_id(value: str) -> str:
    try:
        return file_id(parse_destination_folder_url(value))
    except DriveDestinationError as exc:
        raise DriveVideoError(str(exc)) from None

def folder_ref(value: str) -> tuple[str, str | None]:
    key = folder_id(value)
    parsed = urlparse(value.strip())
    try:
        unusual_authority = parsed.username or parsed.password or parsed.port is not None
    except ValueError:
        raise DriveVideoError("Invalid Google Drive folder URL port.") from None
    if parsed.netloc and unusual_authority:
        raise DriveVideoError("Paste a standard Google Drive folder link without credentials or a port.")
    values = parse_qs(parsed.query).get("resourcekey", [])
    resource = values[0] if values else None
    if len(values) > 1 or (resource and not re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", resource)):
        raise DriveVideoError("Invalid Drive folder resource key. Copy the original folder share link again.")
    return key, resource

def resource_request(request, keys: dict[str, str | None]):
    values = [f"{key}/{value}" for key, value in keys.items() if value]
    if values:
        request.headers = {**getattr(request, "headers", {}), "X-Goog-Drive-Resource-Keys": ",".join(values)}
    return request

def explain(error: Exception) -> str:
    # Do not expose raw Google errors, URLs, credentials or local paths.
    status = _http_status(error)
    if isinstance(error, oauth.DriveScopeRequired):
        return str(error)
    if status == 404:
        return "No access to this Drive folder/file, or it was removed. Check the pasted link and share it with the connected Google account."
    if status == 401 or isinstance(error, oauth.DriveOAuthError):
        return "Google Drive connection expired or is unavailable. Connect Google Drive again, then create a new batch."
    if status == 403:
        return "No permission for this Drive request, or Google's quota was exceeded. Check sharing/download permission; reconnect with read-only consent if needed, then try again."
    if status == 429 or status >= 500:
        return "Google Drive is temporarily unavailable or limiting requests. Completed results are preserved; try a new batch later."
    return "Drive request or download was interrupted. Check the connection and available disk space, then create a new batch."

def connect():
    try:
        service = oauth.get_video_read_service()
        http = getattr(service, "_http", None)
        if http is not None:
            getattr(http, "http", http).timeout = 30
        user = service.about().get(fields="user(displayName,emailAddress,permissionId)").execute(num_retries=0).get("user", {})
        if not user.get("emailAddress"):
            raise DriveVideoError("Cannot verify the connected Google account. Reconnect before selecting videos.")
        return service, dict(display_name=user.get("displayName", ""), email=user["emailAddress"],
            account_id=user.get("permissionId") or user["emailAddress"])
    except DriveVideoError:
        raise
    except Exception as exc:
        raise DriveVideoError(explain(exc)) from None

def check_account(account: dict, expected: str | None) -> None:
    if expected and account["account_id"] != expected:
        raise DriveVideoError("Connected Google account changed. Reload the folder and select its videos again.")

def metadata(service, key: str, resource_keys: dict[str, str | None] | None = None) -> dict:
    try:
        request = service.files().get(fileId=file_id(key), fields=FIELDS, supportsAllDrives=True)
        data = resource_request(request, resource_keys or {}).execute(num_retries=0)
    except Exception as exc:
        if _http_status(exc) == 404:
            raise GrantRequired(explain(exc)) from None
        raise DriveVideoError(explain(exc)) from None
    if data.get("id") != key or data.get("trashed"):
        raise DriveVideoError("Drive file is missing, trashed or changed. Reload the folder.")
    return data

def validate_video(data: dict, parent: str) -> dict:
    if parent not in data.get("parents", []):
        raise DriveVideoError("Selected video is not a direct child of the chosen folder. Select videos from that folder only.")
    try:
        name = video.filename(data.get("name", ""))
    except video.MediaError as exc:
        raise DriveVideoError(str(exc)) from None
    mime = data.get("mimeType", "")
    if mime.startswith("application/vnd.google-apps."):
        raise DriveVideoError("Google documents/shortcuts are not uploaded video files. Select the original video in this folder.")
    # Drive may label MKV (or another valid container) with a generic/unexpected
    # MIME. The supported extension is sufficient to list; probe/decode later
    # checks the actual container and codec, without transcoding.
    try:
        size = int(data.get("size", 0))
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise DriveVideoError("Video size is unavailable or zero. Upload a complete video file with a known size.")
    if data.get("capabilities", {}).get("canDownload") is False:
        raise DriveVideoError("The video owner disabled downloading. Ask the owner to allow downloads.")
    info = data.get("videoMediaMetadata") or {}
    try:
        duration = int(info.get("durationMillis", 0) or 0) / 1000
        pixels = int(info.get("width", 0) or 0) * int(info.get("height", 0) or 0)
    except (TypeError, ValueError):
        raise DriveVideoError("Drive reported invalid duration/dimensions. Refresh the folder after video metadata finishes processing.") from None
    if duration < 0 or pixels < 0:
        raise DriveVideoError("Drive reported invalid duration/dimensions.")
    if duration > MAX_DURATION:
        raise DriveVideoError(f"Video duration is {duration / 3600:.2f} hours; the per-file Drive limit is 12 hours.")
    if pixels > video.MAX_PIXELS:
        raise DriveVideoError("Video resolution exceeds the 3840x2160 pixel limit.")
    return dict(file_id=file_id(data["id"]), filename=name, bytes=size, mime_type=mime,
        modified_time=data.get("modifiedTime"), version=data.get("version"), md5=data.get("md5Checksum"),
        duration_seconds=duration or None, width=info.get("width"), height=info.get("height"), resource_key=data.get("resourceKey"))

def folder_listing(link: str, confirmed_ids: list[str] | None = None, expected_account: str | None = None) -> dict:
    parent, resource = folder_ref(link)
    service, account = connect()
    check_account(account, expected_account)
    keys = {parent: resource}
    folder = metadata(service, parent, keys)
    if folder.get("mimeType") != FOLDER_MIME:
        raise DriveVideoError("This Drive ID is not a folder. Paste a folder link.")
    files, token, listed, subfolders, seen_tokens = {}, None, 0, 0, set()
    try:
        while True:
            params = dict(q=f"'{parent}' in parents and trashed = false",
                fields=f"nextPageToken,incompleteSearch,files({FIELDS})", pageSize=100, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="user")
            if folder.get("driveId"):
                params.update(corpora="drive", driveId=folder["driveId"])
            response = resource_request(service.files().list(**params), keys).execute(num_retries=0)
            if response.get("incompleteSearch"):
                raise DriveVideoError("Google returned an incomplete folder listing. Check Shared Drive access and list again; no batch was started.")
            for data in response.get("files", []):
                listed += 1
                if listed > MAX_LIST_FILES:
                    raise DriveVideoError("Folder is too large to list. Use a folder containing at most 1000 direct files.")
                if data.get("mimeType") == FOLDER_MIME:
                    subfolders += 1
                    continue
                try:
                    item = validate_video(data, parent)
                    item.update(supported=True, error=None)
                except DriveVideoError as exc:
                    # Unsupported candidates visible but not selectable.
                    item = dict(file_id=file_id(data["id"]), filename=str(data.get("name", ""))[:180],
                        bytes=data.get("size"), mime_type=data.get("mimeType", ""), supported=False, error=str(exc))
                item.pop("resource_key", None)
                files[item["file_id"]] = item
            token = response.get("nextPageToken")
            if not token:
                break
            if token in seen_tokens:
                raise DriveVideoError("Drive repeated a pagination token. List the folder again; no batch was started.")
            seen_tokens.add(token)
        for key in set(confirmed_ids or []):
            item = validate_video(metadata(service, key, keys), parent)
            item.pop("resource_key", None)
            files[key] = {**item, "supported": True, "error": None}
    except DriveVideoError:
        raise
    except Exception as exc:
        raise DriveVideoError(explain(exc)) from None
    supported = sum(item["supported"] for item in files.values())
    outcome = "videos" if supported else "unsupported" if files else "no_files" if subfolders else "empty"
    description = {"videos": f"{supported} selectable videos; {len(files) - supported} files cannot be selected.",
        "unsupported": "Files found, but none pass the video validation/limits. See each file's reason below.",
        "no_files": "No direct files found; this folder contains subfolders only.",
        "empty": "This accessible folder has no direct files or subfolders."}[outcome]
    return dict(folder_id=parent, folder_name=str(folder.get("name", ""))[:180], account=account,
        requires_picker_grant=False, files=sorted(files.values(), key=lambda f: (f["filename"].casefold(), f["file_id"])),
        outcome=outcome, direct_child_count=listed, file_count=len(files), supported_count=supported,
        unsupported_count=len(files) - supported, subfolder_count=subfolders, limits=limits(),
        message=description + " Only direct children are listed; subfolders are not scanned.")

def selected_files(link: str, keys: list[str], expected_account: str) -> tuple[str, dict, list[dict]]:
    parent, resource = folder_ref(link)
    if not 1 <= len(keys) <= MAX_FILES or len(set(keys)) != len(keys):
        raise DriveVideoError("Select 1–32 distinct videos. Duplicate file IDs are not allowed.")
    for key in keys:
        file_id(key)
    service, account = connect()
    check_account(account, expected_account)
    if metadata(service, parent, {parent: resource}).get("mimeType") != FOLDER_MIME:
        raise DriveVideoError("Select a Google Drive folder.")
    selected = [{**validate_video(metadata(service, key, {parent: resource}), parent), "parent_resource_key": resource} for key in keys]
    return parent, account, sorted(selected, key=lambda f: (f["filename"].casefold(), f["file_id"]))

def download(item: dict, parent: str, expected_account: str, path: Path, cancelled, progress, claim_key=None) -> dict:
    """One bounded sequential download. Caller deletes .part/media in finally.

    No automatic retries. File version/size/checksum revalidated before media.
    The writer enforces the reported size and the shared disk budget (free
    space minus other transfers' remaining bytes minus the reserve), before
    the transfer and for every chunk, even for misleading HTTP metadata.
    """
    service, account = connect()
    check_account(account, expected_account)
    resource_keys = {parent: item.get("parent_resource_key"), item["file_id"]: item.get("resource_key")}
    current = validate_video(metadata(service, item["file_id"], resource_keys), parent)
    for field in ("bytes", "version", "md5", "modified_time"):
        if current.get(field) != item.get(field):
            raise DriveVideoError("Drive video changed since selection. Reload the folder and create a new batch; this video was not processed.")
    key = claim_key if claim_key is not None else ("download", str(path))
    total = int(item["bytes"])

    def budget(remaining: int) -> None:
        try:
            video.claim_disk(key, remaining, item.get("filename") or "this video", path.parent)
        except video.MediaError as exc:
            raise DriveVideoError(str(exc)) from None

    budget(total)
    digest, md5, written = hashlib.sha256(), hashlib.md5(), 0
    label = item.get("filename") or "this video"
    try:
        try:
            output = path.open("xb")
        except OSError:
            raise DriveVideoError(f"Cannot create the temporary file for {label}. Check local storage permissions.") from None
        with output:
            class BoundedWriter:
                def write(self, chunk):
                    nonlocal written
                    if cancelled():
                        raise DownloadCancelled("Download cancelled; partial local download removed.")
                    if written + len(chunk) > total:
                        raise DriveVideoError("Drive download exceeded its reported size. Partial download removed; reload the folder and create a new batch.")
                    # The file already holds `written` bytes, so only the rest
                    # (including this chunk) still has to fit.
                    budget(total - written)
                    try:
                        output.write(chunk)
                    except OSError:
                        # A local write failure is a storage problem...
                        raise DriveVideoError(f"Cannot save the temporary copy of {label}. Check local disk space and storage permissions.") from None
                    digest.update(chunk); md5.update(chunk); written += len(chunk)
                    return len(chunk)
            request = resource_request(service.files().get_media(fileId=item["file_id"], supportsAllDrives=True), resource_keys)
            reader = MediaIoBaseDownload(BoundedWriter(), request, chunksize=CHUNK_BYTES)
            done = False
            while not done:
                if cancelled():
                    raise DownloadCancelled("Download cancelled; partial local download removed.")
                _, done = reader.next_chunk(num_retries=0)
                progress(written)
        if written != item["bytes"] or (item.get("md5") and md5.hexdigest() != item["md5"]):
            raise DriveVideoError("Drive download was incomplete or its checksum changed. Partial download removed; create a new batch.")
        return dict(bytes=written, sha256=digest.hexdigest())
    except DriveVideoError:
        raise
    except OSError:
        # ...while connection resets, timeouts and TLS errors are OSErrors
        # raised by the transfer itself, not by the disk.
        raise DriveVideoError(f"Network connection to Google Drive was lost while downloading {label} "
            f"({written / 1024 ** 3:.2f} of {total / 1024 ** 3:.2f} GiB). Partial download removed; "
            "check the connection and submit this video again. Disk space was not the cause.") from None
    except Exception as exc:
        raise DriveVideoError(explain(exc)) from None
    finally:
        # Written bytes are now on disk (or removed by the caller): nothing
        # left to reserve. A caller-owned claim is released by the caller.
        if claim_key is None:
            video.release_disk(key)
