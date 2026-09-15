from __future__ import annotations

import base64
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.auth.deps import get_current_user
from app.auth.security import verify_password
from app.config import (
    BACKEND_BASE_URL, FRONTEND_URL, GOOGLE_CLOUD_PROJECT_NUMBER, GOOGLE_DRIVE_CLIENT_ID, GOOGLE_PICKER_API_KEY,
    PHOTO_BATCHES_DIR, PICKER_ALLOWED_ORIGINS, STORAGE_PATH,
)
from app.database.db import get_session
from app.models.models import CleanupLog, Person, PhotoBatch, PhotoBatchFace, PhotoBatchIngestionIssue, PhotoBatchParticipantFolder, PhotoBatchPhoto, User
from app.services.google_drive_folder_service import DriveFolderError, extract_folder_id, folder_link, service_account_email
from app.services.google_drive_oauth_service import (
    DriveOAuthError,
    connected_email,
    exchange_code_and_store,
    generate_pending_state,
    get_auth_url,
    is_connected,
    verify_and_clear_pending_state,
)
from app.services.photo_processing_service import (
    _cancelled, cancel_and_delete_batch, change_retention, create_batch, create_local_batch, retry_unresolved_photos,
    run_photo_batch, upload_staging_dir,
)
from app.services import event_eta_service
from app.services import photo_thumbnail_service
from app.services import batch_edit_lock
from app.services import face_geometry_service
from app.services import mask_style_service
from app.services import export_selection_service
from app.services.photo_processing_service import (
    BatchBusy, ResumeRefused, _cleanup_abandoned_temp, batch_activity, delete_batch, pause_flags, request_pause,
    request_resume,
)
from app.services import photo_regeneration_service
from app.services.photo_source import dedupe_upload_basename, sanitize_upload_basename
from app.services.photo_batch_download_service import (
    TemporaryZipResponse, build_output_zip, local_output_ready, output_manifest,
)
from app.services.drive_destination_service import (
    DriveDestinationError, PICKER_GRANT_PROMPT, PickerGrantRequired, connected_account,
    invalidate_connected_account, parse_destination_folder_url, reserve_upload,
    resolve_folder_ref, run_drive_upload, validate_destination,
)

router = APIRouter(prefix="/api/photo-batches", tags=["photo-batches"])

# Same envelope and page sizes as the already-paginated Uploads/History
# endpoints, so the shared frontend Pagination component just works.
ALLOWED_PAGE_SIZES = (20, 50, 100, 200, 500, 1000)
DEFAULT_PAGE_SIZE = 50


class CreateBatchBody(BaseModel):
    drive_folder_url: str
    retention_days: int = 7
    logo_data_url: str | None = None
    logo_position: str = "bottom-right"
    logo_size: float = 0.15
    # Phase D2 — "blur" (default) | "emoji:<id>" | "image" (+ mask_data_url).
    mask_style: str = "blur"
    mask_data_url: str | None = None


def _resolve_mask(style: str | None, png: bytes | None) -> tuple[str, bytes | None, str | None]:
    """Phase D2 — validate the privacy-mask choice BEFORE any batch exists, so
    an unusable PNG is refused with a clear message instead of silently
    rendering as blur."""
    try:
        return mask_style_service.resolve(style, png)
    except mask_style_service.MaskError as e:
        raise HTTPException(400, str(e))


class RetentionChangeBody(BaseModel):
    password: str
    retention_days: int


class DriveDestinationValidateBody(BaseModel):
    folder_url: str | None = None
    folder_id: str | None = None  # Phase H — the id a Google Picker returned


class DriveUploadBody(BaseModel):
    folder_url: str | None = None
    folder_id: str | None = None  # Phase H — the id a Google Picker returned
    upload_people: bool = True
    upload_media: bool = True
    # Phase O — the same ExportSelection the ZIP uses.
    upload_ambience: bool = False
    person_ids: list[str] | None = None  # None = every matched participant


def _link(folder_id: str) -> str | None:
    return folder_link(folder_id) if folder_id else None


def _eta(b: PhotoBatch) -> tuple[int | None, bool]:
    """Phase G4 — local-processing ETA. Transient and in-memory: only a batch
    actually running in THIS process has one, which is also why it is computed
    at response time rather than stored on the row."""
    if b.status not in ("pending", "processing"):
        return None, False
    remaining = b.total_photos - b.rejected_photos - b.processed_photos - b.failed_photos
    return event_eta_service.eta_seconds(b.id, remaining), event_eta_service.is_estimating(b.id)


def _elapsed_seconds(b: PhotoBatch) -> int | None:
    """Phase G4 — how long the most recent processing run actually took.

    Backend-authoritative and computed from persisted timestamps, so it
    survives a refresh and a restart (unlike the live ETA above). None while a
    run is still going, and None for every batch that finished before those
    columns existed — the UI omits the line rather than inventing a number.
    """
    if not (b.processing_started_at and b.processing_finished_at):
        return None
    elapsed = (b.processing_finished_at - b.processing_started_at).total_seconds()
    return int(elapsed) if elapsed >= 0 else None


# Persisted combined `status` values in which work is genuinely in progress.
# Every value the code can write must appear in exactly one of these two sets:
# test_batch_liveness scans the writers and fails on an unclassified literal,
# so a status added later cannot silently default to "idle" (a stale page) or
# "live" (a page that re-renders every 2 s forever — the People-view stutter).
LIVE_STATUSES = frozenset({"pending", "processing", "pausing", "resuming", "stopping", "uploading", "syncing_drive"})
IDLE_STATUSES = frozenset({"ready", "needs_retry", "failed", "upload_failed", "completed", "review_required",
                           "cancelled", "paused"})


def batch_is_live(b: PhotoBatch) -> bool:
    """Is anything actually happening to this batch right now?

    Runtime truth first — the in-process registries know about work the
    persisted row may not reflect yet (a retry that has just been scheduled, a
    pipeline still draining, a Drive upload holding its reservation). Then the
    persisted axes.

    `local_status` is deliberately NOT consulted: `needs_retry` is mapped to
    local_status PROCESSING for retention protection (see
    `_finalize_batch_status`), but a batch waiting for the admin to press Retry
    is idle, and treating it as live would poll it fast forever.
    """
    from app.services import drive_destination_service as dds
    from app.services import photo_processing_service as pps
    from app.services.event_pipeline_registry import registry as pipeline_registry

    with pps._batch_lock:
        running = b.id in pps._active_batches
    with dds._upload_lock:
        uploading = b.id in dds._uploading_batches
    if running or uploading or pipeline_registry.is_active(b.id):
        return True
    return b.status in LIVE_STATUSES or b.drive_status == "UPLOADING"


def _batch_out(b: PhotoBatch) -> dict:
    eta_seconds, eta_estimating = _eta(b)
    return {
        "id": b.id,
        "eta_seconds": eta_seconds,
        "eta_estimating": eta_estimating,
        "elapsed_seconds": _elapsed_seconds(b),
        # Drives the page's poll rate: fast only while something is live, a
        # slow heartbeat otherwise. Additive; nothing else reads it.
        "live": batch_is_live(b),
        # Pause / Resume / Delete — what the page may offer (+ watchdog while pausing).
        **pause_flags(b),
        "local_status": b.local_status,
        "drive_status": b.drive_status,
        "label": b.label,
        "status": b.status,
        "download_ready": not _cancelled(b.id) and local_output_ready(b, STORAGE_PATH),
        "current_stage": b.current_stage,
        "total_photos": b.total_photos,
        "processed_photos": b.processed_photos,
        "faces_detected": b.faces_detected,
        "faces_recognized": b.faces_recognized,
        "faces_unknown": b.faces_unknown,
        "consented_faces": b.consented_faces,
        "not_consented_faces": b.not_consented_faces,
        "blurred_faces": b.blurred_faces,
        "recognized_photos": b.recognized_photos,
        "ambience_photos": b.ambience_photos,
        "review_photos": b.review_photos,
        "failed_photos": b.failed_photos,
        "rejected_photos": b.rejected_photos,
        "accepted_photos": b.total_photos - b.rejected_photos,
        "last_error": b.last_error,
        "drive_failed_photos": b.drive_failed_photos,
        "drive_error": b.drive_error,
        "retention_days": b.retention_days,
        "retention_start_at": b.retention_start_at,
        "delete_at": b.delete_at,
        "created_at": b.created_at,
        "source_type": b.source_type,
        "source_folder_url": folder_link(b.drive_folder_id) if b.source_type == "drive" else None,
        "processed_folder_url": _link(b.processed_folder_id),
        "media_folder_url": _link(b.media_folder_id),
        "ambience_folder_url": _link(b.ambience_folder_id),
        "review_folder_url": _link(b.review_folder_id),
    }


@router.get("/drive-info")
def drive_info(user: User = Depends(get_current_user)):
    """So the admin knows which email the photographer needs to share the folder with (Viewer is enough — reads go through the service account)."""
    return {"service_account_email": service_account_email()}


@router.get("/drive-oauth/status")
def drive_oauth_status(refresh: bool = False, user: User = Depends(get_current_user)):
    """Which DESTINATION Google account uploads run as — verified against Drive
    itself, so a revoked or expired connection never shows as connected. Never
    the SOURCE service account, and never a token or secret. `email` is kept for
    the batch-list page that already reads it."""
    if not is_connected():
        return {"connected": False, "account": None, "reason": "not_connected", "email": None}
    state = connected_account(force_refresh=refresh)
    account = state.get("account") or {}
    return {**state, "email": account.get("email") or connected_email()}


@router.get("/drive-oauth/start")
def drive_oauth_start(user: User = Depends(get_current_user)):
    """Returns Google's consent-screen URL for the frontend to navigate the
    browser to (can't redirect directly from here — this route requires the
    JWT bearer token, which a plain browser navigation wouldn't send).
    Writes (folder creation, uploads) then run as this connected account,
    not the read-only service account — see google_drive_oauth_service.py."""
    state = generate_pending_state()
    try:
        url = get_auth_url(state)
    except DriveOAuthError as e:
        raise HTTPException(400, str(e))
    return {"auth_url": url}


@router.get("/drive-oauth/callback")
def drive_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Google redirects the bare browser here — no JWT is available, so the
    single-use `state` value (written by /drive-oauth/start) is what proves
    this callback belongs to an admin session that actually initiated it."""
    if error:
        return RedirectResponse(f"{FRONTEND_URL}/photo-batches?drive_error={error}")
    if not code or not state or not verify_and_clear_pending_state(state):
        return RedirectResponse(f"{FRONTEND_URL}/photo-batches?drive_error=invalid_state")
    try:
        exchange_code_and_store(code)
    except DriveOAuthError as e:
        return RedirectResponse(f"{FRONTEND_URL}/photo-batches?drive_error={e}")
    invalidate_connected_account()  # the newly connected account, never the previous one
    return RedirectResponse(f"{FRONTEND_URL}/photo-batches?drive_connected=1")


@router.post("")
def create_photo_batch(
    body: CreateBatchBody,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not (1 <= body.retention_days <= 7):
        raise HTTPException(400, "retention_days must be between 1 and 7")
    # Drive connection is deliberately NOT required — processing and the web
    # preview work without it; only the Drive mirror is skipped.
    try:
        extract_folder_id(body.drive_folder_url)  # validate early, before creating anything
    except DriveFolderError as e:
        raise HTTPException(400, str(e))

    logo_png = None
    if body.logo_data_url:
        prefix = "data:image/png;base64,"
        if not body.logo_data_url.startswith(prefix):
            raise HTTPException(400, "Logo must be a PNG image.")
        try:
            logo_png = base64.b64decode(body.logo_data_url[len(prefix):], validate=True)
        except ValueError as e:
            raise HTTPException(400, "Logo PNG data is invalid.") from e
        if not logo_png or len(logo_png) > 5 * 1024 * 1024:
            raise HTTPException(400, "Logo PNG must be between 1 byte and 5 MB.")
    if body.logo_position not in {"top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"}:
        raise HTTPException(400, "Logo position is invalid.")
    if not 0.02 <= body.logo_size <= 0.5:
        raise HTTPException(400, "Logo size must be between 2% and 50%.")
    uploaded_mask = None
    if body.mask_data_url:
        prefix = "data:image/png;base64,"
        if not body.mask_data_url.startswith(prefix):
            raise HTTPException(400, "The mask must be a PNG image.")
        try:
            uploaded_mask = base64.b64decode(body.mask_data_url[len(prefix):], validate=True)
        except ValueError as e:
            raise HTTPException(400, "Mask PNG data is invalid.") from e
    mask_style, mask_png, mask_source = _resolve_mask(body.mask_style, uploaded_mask)
    batch = create_batch(session, body.drive_folder_url, body.retention_days, user.id, logo_png, body.logo_position, body.logo_size,
                         mask_style=mask_style, mask_png=mask_png, mask_source=mask_source)
    background_tasks.add_task(run_photo_batch, batch.id)
    return _batch_out(batch)


@router.get("")
def list_photo_batches(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    batches = session.exec(select(PhotoBatch).order_by(PhotoBatch.created_at.desc())).all()
    return [_batch_out(b) for b in batches]


@router.get("/hardware-info")
def hardware_info(user: User = Depends(get_current_user)):
    """Read-only hardware snapshot (Phase G1) — feeds the GPU-selection UI
    (Phase G3) and the pipeline's benchmark-profile sizing (Phase B).
    Never fails the request even if detection partially fails; every field
    degrades to a safe value instead. MUST be registered before
    /{batch_id} below, or FastAPI would match "hardware-info" as a
    batch_id and this route would be unreachable."""
    from app.services.hardware_info_service import get_hardware_info

    info = get_hardware_info()
    return {
        "cpu_cores": info.cpu_cores,
        "total_ram_mb": info.total_ram_mb,
        "free_ram_mb": info.free_ram_mb,
        # advertised = what ORT was built with; usable = what actually initialises.
        # Device selection must offer ONLY the verified list — ORT advertises
        # providers whose runtime libraries are absent and silently falls back.
        "available_providers": info.available_providers,
        "usable_providers": info.usable_providers,
        "cuda_available": info.cuda_available,
        "gpus": [
            {"index": g.index, "name": g.name, "total_vram_mb": g.total_vram_mb, "free_vram_mb": g.free_vram_mb}
            for g in info.gpus
        ],
    }


@router.get("/mask-styles")
def mask_styles(user: User = Depends(get_current_user)):
    """Phase D2 — the privacy-mask choices offered when creating a batch: the
    built-in emoji as PNG data URLs (the exact pixels the renderer will use).
    MUST be registered before /{batch_id} (same routing-order rule as
    /hardware-info above)."""
    return {
        "default": "blur",
        "styles": mask_style_service.builtin_catalog(),
        "max_bytes": mask_style_service.MAX_MASK_BYTES,
    }


# ---- Phase H — Google Picker for the DESTINATION folder ---------------------
# The SOURCE (service account, "Photographer's Google Drive") is untouched and
# independent: nothing here can make it fail.

PICKER_HEADER = "x-reconize-picker"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _origin_of(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _picker_request_allowed(request: Request) -> bool:
    """Same-origin, or an explicitly allowed origin. The Origin header (or the
    Referer when a browser omits Origin) must be present — a request with
    neither is refused."""
    source = _origin_of(request.headers.get("origin")) or _origin_of(request.headers.get("referer"))
    if not source:
        return False
    host = (request.headers.get("host") or "").lower()
    if host and source.split("://", 1)[1] == host:
        return True
    allowed = {o for o in (_origin_of(FRONTEND_URL), _origin_of(BACKEND_BASE_URL)) if o}
    allowed.update(o for o in (_origin_of(x) for x in PICKER_ALLOWED_ORIGINS) if o)
    return source in allowed


@router.get("/drive-picker/config")
def drive_picker_config(user: User = Depends(get_current_user)):
    """Non-secret Picker launch values. MUST stay registered before
    /{batch_id}. Never includes the OAuth client secret or any token."""
    missing = [name for name, value in (
        ("GOOGLE_DRIVE_CLIENT_ID", GOOGLE_DRIVE_CLIENT_ID),
        ("GOOGLE_PICKER_API_KEY", GOOGLE_PICKER_API_KEY),
        ("GOOGLE_CLOUD_PROJECT_NUMBER", GOOGLE_CLOUD_PROJECT_NUMBER),
    ) if not value]
    connected = is_connected()
    return {
        "enabled": not missing and connected,
        "connected": connected,
        "missing": missing,
        "client_id": GOOGLE_DRIVE_CLIENT_ID or None,
        "api_key": GOOGLE_PICKER_API_KEY or None,
        "app_id": GOOGLE_CLOUD_PROJECT_NUMBER or None,
    }


@router.post("/drive-picker/token")
def drive_picker_token(request: Request, user: User = Depends(get_current_user)):
    """A short-lived drive.file access token for the Picker session, held only
    in browser memory. Requires the app's Bearer JWT (a header, never an ambient
    cookie), a custom header a cross-site form cannot send, and an allowed
    origin. Responses are no-store and this route logs nothing."""
    if request.headers.get(PICKER_HEADER) != "1":
        raise HTTPException(403, "Missing Picker request header.")
    if not _picker_request_allowed(request):
        raise HTTPException(403, "This page may not request a Google Drive token.")
    from app.services import google_drive_oauth_service as oauth

    try:
        minted = oauth.mint_picker_access_token()
    except oauth.DriveOAuthError as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"access_token": minted["access_token"], "expires_in": minted["expires_in"]},
                        headers=_NO_STORE)


MAX_UPLOAD_FILES = 2000
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024  # 100 MB — generous for one high-res event photo
MAX_UPLOAD_TOTAL_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB per batch — a conservative starting cap, not locked-in policy
_LOGO_POSITIONS = {"top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"}


@router.post("/upload")
def create_local_photo_batch(
    background_tasks: BackgroundTasks,
    label: str = Form(...),
    retention_days: int = Form(7),
    logo_position: str = Form("bottom-right"),
    logo_size: float = Form(0.15),
    logo: UploadFile | None = File(None),
    mask_style: str = Form("blur"),
    mask: UploadFile | None = File(None),
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Phase A2 — local file/folder upload, the second PhotoSource alongside
    Drive. MUST be registered before /{batch_id} below (see hardware-info's
    comment above — same routing-order bug class)."""
    if not (1 <= retention_days <= 7):
        raise HTTPException(400, "retention_days must be between 1 and 7")
    if not label.strip():
        raise HTTPException(400, "Batch label is required.")
    if logo_position not in _LOGO_POSITIONS:
        raise HTTPException(400, "Logo position is invalid.")
    if not 0.02 <= logo_size <= 0.5:
        raise HTTPException(400, "Logo size must be between 2% and 50%.")
    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(400, "Select at least one photo to upload.")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"Cannot upload more than {MAX_UPLOAD_FILES} files in one batch.")
    total_bytes = 0
    for f in files:
        if f.size is not None and f.size > MAX_UPLOAD_FILE_BYTES:
            raise HTTPException(400, f"{f.filename} exceeds the {MAX_UPLOAD_FILE_BYTES // (1024 * 1024)} MB per-file limit.")
        total_bytes += f.size or 0
    if total_bytes > MAX_UPLOAD_TOTAL_BYTES:
        raise HTTPException(400, f"Total upload exceeds the {MAX_UPLOAD_TOTAL_BYTES // (1024 ** 3)} GB per-batch limit.")

    logo_png = None
    if logo is not None and logo.filename:
        logo_png = logo.file.read()
        if not logo_png or len(logo_png) > 5 * 1024 * 1024:
            raise HTTPException(400, "Logo PNG must be between 1 byte and 5 MB.")
    uploaded_mask = None
    if mask is not None and mask.filename:
        # One byte over the cap is enough for validation to refuse it.
        uploaded_mask = mask.file.read(mask_style_service.MAX_MASK_BYTES + 1)
    resolved_style, mask_png, mask_source = _resolve_mask(mask_style, uploaded_mask)

    batch = create_local_batch(session, label.strip(), retention_days, user.id, logo_png, logo_position, logo_size,
                               mask_style=resolved_style, mask_png=mask_png, mask_source=mask_source)

    staging = upload_staging_dir(batch.id)
    staging.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    for index, upload in enumerate(files, start=1):
        try:
            safe_name = sanitize_upload_basename(upload.filename)
        except ValueError:
            safe_name = f"upload_{index}.bin"  # never silently drop a submitted file — let it flow through
        staged_name = dedupe_upload_basename(safe_name, taken)
        with (staging / staged_name).open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                out.write(chunk)

    if _cancelled(batch.id):
        # Stop was pressed while the files were still arriving: never start it.
        _cleanup_abandoned_temp(batch.id)
        return _batch_out(batch)
    background_tasks.add_task(run_photo_batch, batch.id)
    return _batch_out(batch)


@router.get("/{batch_id}")
def get_photo_batch(batch_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    b = session.get(PhotoBatch, batch_id)
    if not b:
        raise HTTPException(404, "Photo batch not found")
    return _batch_out(b)


@router.post("/{batch_id}/pause")
def pause_photo_batch(batch_id: str, user: User = Depends(get_current_user)):
    """Cooperative pause: completed work and pending items are kept. Returns
    at once; the page polls until status is "paused". Idempotent."""
    result = request_pause(batch_id)
    if result["status"] == "not_found":
        raise HTTPException(404, "Photo batch not found")
    return result


@router.post("/{batch_id}/resume")
def resume_photo_batch(batch_id: str, user: User = Depends(get_current_user)):
    """Continue a paused batch from where it stopped. Idempotent for a batch
    already resuming/processing; 409 with the reason for anything else."""
    try:
        result = request_resume(batch_id)
    except ResumeRefused as e:
        raise HTTPException(409, str(e))
    if result["status"] == "not_found":
        raise HTTPException(404, "Photo batch not found")
    return result


@router.delete("/{batch_id}")
def delete_photo_batch(batch_id: str, user: User = Depends(get_current_user)):
    """Follow-up Task 2 — deletes a stopped or finished batch only. 409 with the
    exact reason while anything is running for it; an already-deleted batch is
    reported ({"already_deleted": true}), not an error."""
    try:
        return delete_batch(batch_id)
    except BatchBusy as e:
        raise HTTPException(409, str(e))


@router.get("/{batch_id}/download")
def download_photo_batch(batch_id: str, select_categories: str | None = Query(None, alias="select"),
                         person_ids: str | None = None,
                         session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Without `select`: the full ZIP exactly as before (legacy regime — a
    historical batch keeps its REVIEW/ folder). With `select=media,sorted,
    ambience` (+ optional `person_ids`): Phase O's shared ExportSelection,
    applied after the same validation; never REVIEW/ORIGINAL/THUMBNAILS."""
    batch = session.get(PhotoBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Photo batch not found")
    if _cancelled(batch_id):
        raise HTTPException(409, "Batch is stopping or deleting.")
    # Phase D1 — a face-box edit replaces derived files; a ZIP built at the
    # same moment could capture a mix of old and new output. Refused while an
    # edit holds the batch (and an edit is refused while this runs).
    selection = None
    # Named select_categories (URL name stays ?select=): a parameter called
    # `select` would shadow sqlmodel.select used below and break every download.
    if select_categories is not None:
        try:
            selection = export_selection_service.parse_query(select_categories, person_ids)
        except export_selection_service.ExportSelectionError as e:
            raise HTTPException(400, str(e))
    if not batch_edit_lock.begin_download(batch_id):
        raise HTTPException(409, "A face-box edit is being saved for this batch. Try the download again in a moment.")
    try:
        photos = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all()
        if selection is None:
            root, files = output_manifest(batch, photos, STORAGE_PATH)
        else:
            folders = export_selection_service.sorted_folder_names(session, selection.person_ids)
            try:
                root, files = export_selection_service.zip_manifest(batch, photos, STORAGE_PATH, selection, folders)
            except export_selection_service.ExportSelectionError as e:
                raise HTTPException(400, str(e))
        # End the read transaction before potentially large ZIP work. No DB
        # writes, cancellation lock or Drive calls while preparing/sending it.
        session.close()
        # Phase O — a selection gets folder entries only for what it contains.
        archive = build_output_zip(root, files, folders=None if selection is None else
                                   sorted({f.relative_to(root).parts[0] for f in files}))
    except (OSError, ValueError) as e:
        raise HTTPException(409, "Local output is not ready or changed during download. Refresh and retry.") from e
    finally:
        # The archive is a complete temp file by now; streaming it no longer
        # reads the batch's live files.
        batch_edit_lock.end_download(batch_id)
    return TemporaryZipResponse(archive, media_type="application/zip", filename=f"event-photos-{batch_id}.zip")


@router.post("/{batch_id}/retry-unresolved")
def retry_unresolved(
    batch_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Re-attempts only the photos counted in failed_photos (unresolved) —
    never rejected_photos items, which can never be retried into existence.
    Re-reads each photo's own ORIGINAL/{filename}, never the Drive source."""
    batch = session.get(PhotoBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Photo batch not found")
    if batch.status in ("pausing", "paused", "resuming"):
        raise HTTPException(409, "This batch is paused. Resume it to continue processing.")
    if batch.status in ("cancelled", "stopping"):
        raise HTTPException(409, "This batch was stopped. Delete it and start a new batch to process these photos again.")
    if _cancelled(batch_id):
        raise HTTPException(409, "Batch is stopping or deleting.")
    if batch.failed_photos <= 0:
        raise HTTPException(400, "Nothing to retry.")
    background_tasks.add_task(retry_unresolved_photos, batch_id)
    return {"status": "processing"}


@router.get("/{batch_id}/ingestion-issues")
def list_ingestion_issues(batch_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Rejected-item detail: filename + reason, never overwritten the way a
    single last_error string would be. Permanent — these items are never
    retried and never block ready."""
    if not session.get(PhotoBatch, batch_id):
        raise HTTPException(404, "Photo batch not found")
    issues = session.exec(
        select(PhotoBatchIngestionIssue)
        .where(PhotoBatchIngestionIssue.batch_id == batch_id)
        .order_by(PhotoBatchIngestionIssue.occurred_at)
    ).all()
    return [{"filename": i.filename, "reason": i.reason, "occurred_at": i.occurred_at} for i in issues]


@router.post("/{batch_id}/drive-destination/validate")
def validate_drive_destination(
    batch_id: str,
    body: DriveDestinationValidateBody,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Read-only check: does the EXISTING Drive write identity already have
    access to this pasted folder? Never expands OAuth scope and never
    uploads anything — see drive_destination_service.py for why a pasted
    folder not created by this app usually cannot be validated."""
    if not session.get(PhotoBatch, batch_id):
        raise HTTPException(404, "Photo batch not found")
    try:
        folder_id = resolve_folder_ref(body.folder_url, getattr(body, "folder_id", None))
        return validate_destination(folder_id)
    except PickerGrantRequired as e:
        # Not an OAuth failure: under drive.file Google needs ONE Picker
        # confirmation for a folder Reconize has never been granted. The pasted
        # id travels back so the page can ask for exactly that folder.
        return {"requires_picker_grant": True, "pasted_folder_id": e.folder_id, "message": PICKER_GRANT_PROMPT}
    except DriveDestinationError as e:
        raise HTTPException(400, str(e))


@router.post("/{batch_id}/drive-upload")
def start_drive_upload(
    batch_id: str,
    body: DriveUploadBody,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Explicit, user-initiated Drive upload of the batch's existing local
    SORTED (Person) and MEDIA output only — never REVIEW/ORIGINAL/SOURCE, and
    never a re-run of detection/recognition/blur/logo. Destination is
    revalidated here, server-side, regardless of what the frontend already
    checked."""
    batch = session.get(PhotoBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Photo batch not found")
    if _cancelled(batch_id):
        raise HTTPException(409, "Batch is stopping or deleting.")
    if not local_output_ready(batch, STORAGE_PATH):
        raise HTTPException(409, "Local processing output is not ready yet.")
    upload_ambience = bool(getattr(body, "upload_ambience", False))
    if not (body.upload_people or body.upload_media or upload_ambience):
        raise HTTPException(400, "Select at least one of People (SORTED), MEDIA or AMBIENCE to upload.")
    sorted_folders = export_selection_service.sorted_folder_names(session, getattr(body, "person_ids", None))
    if sorted_folders is not None and body.upload_people and not sorted_folders:
        raise HTTPException(400, "Choose at least one participant, or include all participants.")
    try:
        folder_id = resolve_folder_ref(body.folder_url, getattr(body, "folder_id", None))
        validate_destination(folder_id)
    except DriveDestinationError as e:
        raise HTTPException(400, str(e))
    # Reserved through the D1 activity lock, so an upload cannot start while a
    # face-box edit is replacing this batch's files (and vice versa).
    if not batch_edit_lock.try_reserve_upload(batch_id):
        raise HTTPException(409, "An upload for this batch is already running, or a face-box edit is being saved.")
    background_tasks.add_task(run_drive_upload, batch_id, folder_id, body.upload_people, body.upload_media,
                              upload_ambience, sorted_folders)
    return {"status": "uploading"}


@router.get("/{batch_id}/participants")
def list_batch_participants(batch_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    b = session.get(PhotoBatch, batch_id)
    if not b:
        raise HTTPException(404, "Photo batch not found")

    rows = session.exec(
        select(PhotoBatchParticipantFolder).where(PhotoBatchParticipantFolder.batch_id == batch_id)
    ).all()
    out = []
    for row in rows:
        p = session.get(Person, row.person_id)
        if p:
            out.append({
                "person_id": p.id,
                "participant_id": p.participant_id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "photo_count": row.photo_count,
                "folder_url": _link(row.folder_id),  # None when the Drive mirror was skipped/failed
            })
    out.sort(key=lambda r: r["participant_id"])
    return out


@router.get("/{batch_id}/photos")
def list_batch_photos(
    batch_id: str,
    classification: str | None = None,
    person_id: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Backs the in-app web preview. Returns local storage paths (served by
    the existing /api/files/{path} route) — deliberately independent of
    Google Drive, so the preview keeps working when Drive is unavailable.
    No embeddings and no consent details are exposed here."""
    b = session.get(PhotoBatch, batch_id)
    if not b:
        raise HTTPException(404, "Photo batch not found")

    page = max(page, 1)
    if page_size not in ALLOWED_PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE

    # Phase I4 — every filter is now applied in SQL. The person_id filter
    # previously loaded EVERY face for that person across ALL batches, then
    # every photo in this batch, and intersected them in Python; a crowd batch
    # made that two full scans per tab click. It is one indexed subquery now,
    # and it is finally scoped to this batch.
    stmt = select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)
    if classification:
        stmt = stmt.where(PhotoBatchPhoto.classification == classification)
    if person_id:
        stmt = stmt.where(
            PhotoBatchPhoto.id.in_(  # type: ignore[attr-defined]
                select(PhotoBatchFace.photo_id).where(PhotoBatchFace.person_id == person_id)
            )
        )

    total = session.exec(
        select(func.count()).select_from(stmt.subquery())
    ).one()
    # Stable ordering — without it, offset/limit paging can repeat or skip
    # rows between pages.
    photos = session.exec(
        stmt.order_by(PhotoBatchPhoto.filename).offset((page - 1) * page_size).limit(page_size)
    ).all()

    # Phase I4/J1 — the grid renders `thumbnail_path`, not the ~5 MB artifact.
    # This only builds the URL: the thumbnail itself is generated on demand by
    # the file route when the browser asks for it, so a batch that predates
    # thumbnails needs no backfill and this response stays fast. The URL is
    # versioned by the MEDIA artifact's mtime, which is what the thumbnail is
    # derived from — that is what makes files.py's immutable caching safe.
    def _thumb(p: PhotoBatchPhoto) -> str | None:
        return photo_thumbnail_service.thumbnail_url(
            b.storage_dir, p.filename, STORAGE_PATH, p.media_path)

    return {
        "items": [
            {
                "id": p.id,
                "filename": p.filename,
                "classification": p.classification,
                "faces_total": p.faces_total,
                "faces_matched": p.faces_matched,
                "faces_unknown": p.faces_unknown,
                "original_path": p.original_path,
                "media_path": p.media_path,
                "thumbnail_path": _thumb(p),
                "drive_upload_status": p.drive_upload_status,
                "drive_error": p.drive_error,
            }
            for p in photos
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _batch_photo_or_404(session: Session, batch_id: str, photo_id: str) -> tuple[PhotoBatch, PhotoBatchPhoto]:
    batch = session.get(PhotoBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Photo batch not found")
    photo = session.get(PhotoBatchPhoto, photo_id)
    if not photo or photo.batch_id != batch_id:
        raise HTTPException(404, "Photo not found in this batch")
    return batch, photo


class FaceBoxBody(BaseModel):
    # [x1, y1, x2, y2] in ORIGINAL-image pixels — the only space accepted.
    bbox: list[float]


@router.get("/{batch_id}/photos/{photo_id}/faces")
def get_photo_faces(batch_id: str, photo_id: str, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)):
    """Phase D1 — one photo's face boxes for the bounding-box editor, in
    ORIGINAL-image coordinates, plus the privacy-rendered MEDIA path to display.
    Never returns an ORIGINAL path: declined faces stay masked in the editor."""
    batch, photo = _batch_photo_or_404(session, batch_id, photo_id)
    if not photo.media_path:
        raise HTTPException(409, "This photo has not finished processing.")
    return face_geometry_service.faces_payload(session, batch, photo, STORAGE_PATH)


@router.put("/{batch_id}/photos/{photo_id}/faces/{face_id}")
def put_face_box(batch_id: str, photo_id: str, face_id: str, body: FaceBoxBody,
                 session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Phase D1 — save one corrected box (ORIGINAL-image pixels) and re-render
    that photo's derived outputs at full resolution. Geometry only: never an
    identity, a consent value or a visibility decision. Any failure leaves the
    database and every file exactly as they were."""
    batch, photo = _batch_photo_or_404(session, batch_id, photo_id)
    if _cancelled(batch_id):
        raise HTTPException(409, "Batch is stopping or deleting.")
    if len(body.bbox) != 4:
        raise HTTPException(400, "A box needs four numbers: x1, y1, x2, y2.")
    try:
        face = face_geometry_service.update_face_bbox(
            session, batch, photo, face_id, tuple(body.bbox),
            storage_path=STORAGE_PATH, photo_batches_dir=PHOTO_BATCHES_DIR,
        )
    except face_geometry_service.GeometryError as e:
        raise HTTPException(e.status, str(e))
    except photo_regeneration_service.RegenerationError as e:
        raise HTTPException(500, f"The photo could not be re-rendered, so nothing was changed: {e}")
    session.refresh(photo)
    return {
        "id": face.id,
        "bbox": list(face_geometry_service.parse_bbox(face.bbox)),
        "detected_bbox": list(face_geometry_service.parse_bbox(face.detected_bbox)) if face.detected_bbox else None,
        "thumbnail_path": photo_thumbnail_service.thumbnail_url(batch.storage_dir, photo.filename, STORAGE_PATH,
                                                                photo.media_path),
    }


@router.put("/{batch_id}/retention")
def update_retention(
    batch_id: str,
    body: RetentionChangeBody,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Requires re-entering the current admin's password — being logged in
    (which every endpoint here already requires) is not enough on its own
    for changing how long event data is retained."""
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect password")

    batch = session.get(PhotoBatch, batch_id)
    if not batch:
        raise HTTPException(404, "Photo batch not found")

    try:
        batch = change_retention(session, batch, body.retention_days)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _batch_out(batch)


cleanup_router = APIRouter(prefix="/api/cleanup-logs", tags=["photo-batches"])


@cleanup_router.get("")
def list_cleanup_logs(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    logs = session.exec(select(CleanupLog).order_by(CleanupLog.deleted_at.desc())).all()
    return logs
