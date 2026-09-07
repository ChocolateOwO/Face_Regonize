from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/history", tags=["history"])

# Same options offered on every paginated list in the app (Upload History
# mirrors this) — a fixed set rather than an arbitrary integer keeps a
# mistyped page_size from someone hand-editing the URL into a
# one-page-of-50000 request, which is the exact slowness this replaces.
ALLOWED_PAGE_SIZES = (20, 50, 100, 200, 500, 1000)
DEFAULT_PAGE_SIZE = 50


@router.get("")
def recognition_history(
    q: str | None = None,
    status: str | None = None,
    person_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    page = max(page, 1)
    if page_size not in ALLOWED_PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE

    # Person/Upload are joined here — not looked up per-row afterward — so
    # filtering, counting and paging all happen in the database. With tens
    # of thousands of history rows, a per-row session.get() for both the
    # matched Person and the source Upload was two extra queries EACH,
    # which is what made this page slow; that N+1 is gone, not just capped.
    stmt = (
        select(FaceDetection, Person, Upload)
        .outerjoin(Person, FaceDetection.person_id == Person.id)
        .outerjoin(Upload, FaceDetection.upload_id == Upload.id)
    )
    if status:
        stmt = stmt.where(FaceDetection.status == status)
    if person_id:
        stmt = stmt.where(FaceDetection.person_id == person_id)
    if date_from:
        stmt = stmt.where(FaceDetection.detected_at >= datetime.fromisoformat(date_from))
    if date_to:
        stmt = stmt.where(FaceDetection.detected_at <= datetime.fromisoformat(date_to))
    if q:
        like = f"%{q}%"
        full_name = Person.first_name.concat(" ").concat(Person.last_name)
        stmt = stmt.where(or_(
            full_name.ilike(like),
            Person.participant_id.ilike(like),
            Upload.filename.ilike(like),
        ))

    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()

    paged = stmt.order_by(FaceDetection.detected_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = session.exec(paged).all()

    out = []
    for d, person, upload in rows:
        name = f"{person.first_name} {person.last_name}".strip() if person else "Unknown"
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
    return {"items": out, "total": total, "page": page, "page_size": page_size}


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
