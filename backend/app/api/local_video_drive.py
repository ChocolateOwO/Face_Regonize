"""Admin-only Drive input endpoints for the video experiment.

OAuth reuses the existing project/callback, adding only video read access.
Picker remains available for compatible clients and other Drive features.
Runner uses no Event Batch
API, database records, destination upload or legacy photo workflow.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from app.api.local_video_experiment import require_admin
from app.models.models import User
from app.services import local_video_experiment as video
from app.services import drive_video_source as drive, drive_video_batch as batch
from app.services.scan_settings import ScanSettings
from app.services import google_drive_oauth_service as oauth
from app.api.scan_settings import ScanSettingsRoute

router = APIRouter(prefix="/api/local-video-experiment/drive", tags=["local-video-drive"], route_class=ScanSettingsRoute)

class FolderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder_link: str = Field(min_length=1, max_length=500)
    confirmed_file_ids: list[str] = Field(default_factory=list, max_length=drive.MAX_FILES)
    account_id: str | None = Field(default=None, max_length=200)

class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    folder_link: str = Field(min_length=1, max_length=500)
    file_ids: list[str] = Field(min_length=1, max_length=drive.MAX_FILES)
    account_id: str = Field(min_length=1, max_length=200)
    scan_settings: ScanSettings = Field(default_factory=ScanSettings)

@router.get("/status")
def status(refresh: bool = False, user: User = Depends(require_admin)):
    from app.api.photo_batches import drive_oauth_status
    state = drive_oauth_status(refresh=refresh, user=user)
    ready = bool(state["connected"] and oauth.has_video_read_scope())
    return {**state, "read_access": ready, "requires_reconnect": not ready,
        "required_scope": oauth.VIDEO_READ_SCOPE, "limits": drive.limits(),
        "message": "Read-only video access is ready." if ready else
            "Reconnect Google Drive once and allow read-only access. Existing drive.file access for other features is retained."}

@router.get("/oauth/start")
def oauth_start(user: User = Depends(require_admin)):
    try:
        url = oauth.get_auth_url(oauth.generate_pending_state(), scopes=oauth.VIDEO_SCOPES)
        return {"auth_url": url}
    except oauth.DriveOAuthError as exc:
        raise HTTPException(400, "Google Drive OAuth is not configured. Check the existing OAuth client configuration.") from exc

@router.get("/picker/config")
def picker_config(user: User = Depends(require_admin)):
    from app.api.photo_batches import drive_picker_config
    return drive_picker_config(user=user)

@router.post("/picker/token")
def picker_token(request: Request, user: User = Depends(require_admin)):
    from app.api.photo_batches import drive_picker_token
    return drive_picker_token(request=request, user=user)

@router.post("/folder")
def folder(body: FolderRequest, user: User = Depends(require_admin)):
    try:
        return drive.folder_listing(body.folder_link, body.confirmed_file_ids, body.account_id)
    except drive.DriveVideoError as exc:
        raise HTTPException(400, str(exc))

@router.post("/batches", status_code=201)
def create(body: BatchRequest, user: User = Depends(require_admin)):
    try:
        return video.public(batch.create(body.name, body.folder_link, body.file_ids, body.account_id, body.scan_settings))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
    except (drive.DriveVideoError, video.MediaError) as exc:
        raise HTTPException(400, str(exc))
