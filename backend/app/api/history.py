from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def recognition_history(
    q: str | None = None,
    status: str | None = None,
    person_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    stmt = select(FaceDetection).order_by(FaceDetection.detected_at.desc())
    if status:
        stmt = stmt.where(FaceDetection.status == status)
    if person_id:
        stmt = stmt.where(FaceDetection.person_id == person_id)
    if date_from:
        stmt = stmt.where(FaceDetection.detected_at >= datetime.fromisoformat(date_from))
    if date_to:
        stmt = stmt.where(FaceDetection.detected_at <= datetime.fromisoformat(date_to))

    detections = session.exec(stmt).all()

    out = []
    for d in detections:
        person = session.get(Person, d.person_id) if d.person_id else None
        upload = session.get(Upload, d.upload_id)
        name = f"{person.first_name} {person.last_name}".strip() if person else "Unknown"

        if q:
            ql = q.lower()
            haystack = " ".join(filter(None, [
                name.lower(),
                person.participant_id.lower() if person else "",
                upload.filename.lower() if upload else "",
            ]))
            if ql not in haystack:
                continue

        out.append({
            "id": d.id,
            "upload_id": d.upload_id,
            "upload_filename": upload.filename if upload else None,
            "person_id": d.person_id,
            "name": name if person else None,
            "participant_id": person.participant_id if person else None,
            "confidence": d.confidence,
            "status": d.status,
            "detected_at": d.detected_at,
        })
    return out


@router.delete("")
def delete_all_history(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Deletes every recognition history entry (all FaceDetection rows) —
    same scope as deleting them one at a time, just all at once. Does not
    touch Upload, Person, or Attendance rows, same as the single-entry
    delete above."""
    detections = session.exec(select(FaceDetection)).all()
    for d in detections:
        session.delete(d)
    session.commit()
    return {"deleted": len(detections)}


@router.delete("/{detection_id}")
def delete_history_entry(detection_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Deletes one recognition history entry (FaceDetection row) only — does
    not touch the parent Upload, the Person, or any Attendance row it may
    have produced, matching the same Upload/History/Attendance relationship
    already established by the Settings page's bulk "Clear Recognition
    History" action. The parent Upload's stored faces_total/matched/unknown
    counts are left as-is (a snapshot of the original scan), not recomputed."""
    d = session.get(FaceDetection, detection_id)
    if not d:
        raise HTTPException(404, "History entry not found")
    session.delete(d)
    session.commit()
    return {"deleted": True}
