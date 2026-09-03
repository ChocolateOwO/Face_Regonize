from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Attendance, Person, User

router = APIRouter(prefix="/api/attendees", tags=["attendees"])


@router.get("")
def list_attendees(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    people = session.exec(select(Person)).all()
    out = []
    for p in people:
        detections = session.exec(select(Attendance).where(Attendance.person_id == p.id)).all()
        times = sorted(d.detected_at for d in detections)
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
            "detection_count": len(detections),
            "first_detected": times[0] if times else None,
            "last_detected": times[-1] if times else None,
            "status": "in_event" if times else "not_detected",
        })
    return out
