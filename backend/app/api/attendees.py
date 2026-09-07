from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Attendance, Person, User

router = APIRouter(prefix="/api/attendees", tags=["attendees"])


@router.get("")
def list_attendees(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    people = session.exec(select(Person)).all()
    summary_rows = session.exec(select(Attendance.person_id, func.count(Attendance.id), func.min(Attendance.detected_at), func.max(Attendance.detected_at)).group_by(Attendance.person_id)).all()
    summaries = {person_id: (count, first, last) for person_id, count, first, last in summary_rows}
    out = []
    for p in people:
        detection_count, first_detected, last_detected = summaries.get(p.id, (0, None, None))
        out.append({
            "id": p.id,
            "participant_id": p.participant_id,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "image_path": p.image_path,
            # Lets the UI version the photo url: the file lives at the fixed
            # path people/{id}/profile.jpg and is overwritten in place, so
            # without this the browser can show the previous occupant's face.
            "updated_at": p.updated_at,
            "detection_count": detection_count,
            "first_detected": first_detected,
            "last_detected": last_detected,
            "status": "in_event" if detection_count else "not_detected",
        })
    return out
