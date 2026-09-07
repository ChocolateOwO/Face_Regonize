from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.config import THUMBNAILS_DIR
from app.database.db import get_session
from app.models.models import FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# Same fixed set offered on Recognition History — pagination exists here for
# the same reason: this page renders one <img> thumbnail per row, and with
# tens of thousands of uploads, fetching and rendering all of them at once
# is what made the page slow.
ALLOWED_PAGE_SIZES = (20, 50, 100, 200, 500, 1000)
DEFAULT_PAGE_SIZE = 50


def _thumbnail_path(image_path: str) -> str:
    """storage_service.save_event_image() already writes a 320x320 JPEG for
    EVERY event photo, under the SAME basename, flat inside THUMBNAILS_DIR
    (no date subfolder) — see storage_service.py. That thumbnail path was
    generated but never persisted anywhere, so the grid was rendering full-
    resolution originals for every card. Reconstructing it from image_path
    needs no new column and no backfill — it already exists on disk for
    every upload ever made. Falls back to the full image only if the
    thumbnail file is somehow missing (e.g. a very old upload from before
    thumbnailing existed, or thumbnailing failed for that one file)."""
    thumb = THUMBNAILS_DIR / Path(image_path).name
    return f"thumbnails/{thumb.name}" if thumb.exists() else image_path


def _upload_out(u: Upload) -> dict:
    return {
        "id": u.id,
        "filename": u.filename,
        "image_path": u.image_path,
        "thumbnail_path": _thumbnail_path(u.image_path),
        "uploaded_at": u.uploaded_at,
        "uploaded_by": u.uploaded_by,
        "processing_status": u.processing_status,
        "processing_duration_ms": u.processing_duration_ms,
        "threshold_used": u.threshold_used,
        "faces_total": u.faces_total,
        "faces_matched": u.faces_matched,
        "faces_unknown": u.faces_unknown,
    }


@router.get("")
def list_uploads(
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    page = max(page, 1)
    if page_size not in ALLOWED_PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE

    total = session.exec(select(func.count()).select_from(Upload)).one()
    stmt = (
        select(Upload)
        .order_by(Upload.uploaded_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    uploads = session.exec(stmt).all()
    return {
        "items": [_upload_out(u) for u in uploads],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{upload_id}")
def get_upload(upload_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    u = session.get(Upload, upload_id)
    if not u:
        raise HTTPException(404, "Upload not found")

    detections = session.exec(select(FaceDetection).where(FaceDetection.upload_id == upload_id)).all()
    detection_out = []
    for d in detections:
        person = session.get(Person, d.person_id) if d.person_id else None
        detection_out.append({
            "id": d.id,
            "bbox": [float(v) for v in d.bbox.split(",")],
            "confidence": d.confidence,
            "status": d.status,
            "detected_at": d.detected_at,
            "person_id": d.person_id,
            "name": f"{person.first_name} {person.last_name}".strip() if person else None,
            "participant_id": person.participant_id if person else None,
        })

    out = _upload_out(u)
    out["detections"] = detection_out
    return out


@router.delete("/{upload_id}")
def delete_upload(upload_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Deletes an Upload and its child FaceDetection rows together (an
    orphaned FaceDetection pointing at a deleted Upload has nothing sensible
    to display as its source image, so unlike the History-entry delete this
    one does cascade). Any Attendance rows produced from those detections are
    left untouched, same as the Settings page's bulk clear actions."""
    u = session.get(Upload, upload_id)
    if not u:
        raise HTTPException(404, "Upload not found")
    detections = session.exec(select(FaceDetection).where(FaceDetection.upload_id == upload_id)).all()
    for d in detections:
        session.delete(d)
    session.delete(u)
    session.commit()
    return {"deleted": True}
