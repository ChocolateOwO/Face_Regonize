from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.auth.security import verify_password
from app.config import FRONTEND_URL
from app.database.db import get_session
from app.models.models import CleanupLog, Person, PhotoBatch, PhotoBatchFace, PhotoBatchParticipantFolder, PhotoBatchPhoto, User
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
from app.services.photo_processing_service import change_retention, create_batch, run_photo_batch

router = APIRouter(prefix="/api/photo-batches", tags=["photo-batches"])


class CreateBatchBody(BaseModel):
    drive_folder_url: str
    retention_days: int = 7


class RetentionChangeBody(BaseModel):
    password: str
    retention_days: int


def _link(folder_id: str) -> str | None:
    return folder_link(folder_id) if folder_id else None


def _batch_out(b: PhotoBatch) -> dict:
    return {
        "id": b.id,
        "label": b.label,
        "status": b.status,
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
        "last_error": b.last_error,
        "drive_failed_photos": b.drive_failed_photos,
        "drive_error": b.drive_error,
        "retention_days": b.retention_days,
        "retention_start_at": b.retention_start_at,
        "delete_at": b.delete_at,
        "created_at": b.created_at,
        "source_folder_url": folder_link(b.drive_folder_id),
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
def drive_oauth_status(user: User = Depends(get_current_user)):
    return {"connected": is_connected(), "email": connected_email()}


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

    batch = create_batch(session, body.drive_folder_url, body.retention_days, user.id)
    background_tasks.add_task(run_photo_batch, batch.id)
    return _batch_out(batch)


@router.get("")
def list_photo_batches(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    batches = session.exec(select(PhotoBatch).order_by(PhotoBatch.created_at.desc())).all()
    return [_batch_out(b) for b in batches]


@router.get("/{batch_id}")
def get_photo_batch(batch_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    b = session.get(PhotoBatch, batch_id)
    if not b:
        raise HTTPException(404, "Photo batch not found")
    return _batch_out(b)


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

    if person_id:
        photo_ids = {
            f.photo_id
            for f in session.exec(select(PhotoBatchFace).where(PhotoBatchFace.person_id == person_id)).all()
        }
        photos = session.exec(select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)).all()
        photos = [p for p in photos if p.id in photo_ids]
    else:
        stmt = select(PhotoBatchPhoto).where(PhotoBatchPhoto.batch_id == batch_id)
        if classification:
            stmt = stmt.where(PhotoBatchPhoto.classification == classification)
        photos = session.exec(stmt).all()

    return [
        {
            "id": p.id,
            "filename": p.filename,
            "classification": p.classification,
            "faces_total": p.faces_total,
            "faces_matched": p.faces_matched,
            "faces_unknown": p.faces_unknown,
            "original_path": p.original_path,
            "media_path": p.media_path,
            "drive_upload_status": p.drive_upload_status,
            "drive_error": p.drive_error,
        }
        for p in photos
    ]


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
