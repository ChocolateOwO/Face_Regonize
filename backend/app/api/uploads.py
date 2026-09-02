from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


def _upload_out(u: Upload) -> dict:
    return {
        "id": u.id,
        "filename": u.filename,
        "image_path": u.image_path,
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
def list_uploads(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    uploads = session.exec(select(Upload).order_by(Upload.uploaded_at.desc())).all()
    return [_upload_out(u) for u in uploads]


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
