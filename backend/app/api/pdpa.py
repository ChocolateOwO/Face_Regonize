from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import ConsentRecord, Person, User

router = APIRouter(prefix="/api/pdpa", tags=["pdpa"])


class RecordConsentBody(BaseModel):
    person_ids: list[str]
    choice: str  # "consented" | "declined"
    source: str = "kiosk"  # "kiosk" | "admin" — who made this consent decision


def _latest_per_person(session: Session, person_ids: list[str] | None = None) -> dict[str, ConsentRecord]:
    """Latest ConsentRecord per person_id — one query, grouped in Python since
    the table stays small (one row per consent action, not per scan)."""
    stmt = select(ConsentRecord).order_by(ConsentRecord.recorded_at.asc())
    if person_ids is not None:
        stmt = stmt.where(ConsentRecord.person_id.in_(person_ids))
    latest: dict[str, ConsentRecord] = {}
    for record in session.exec(stmt).all():
        latest[record.person_id] = record  # later rows overwrite earlier ones — ascending order means the last write wins
    return latest


@router.post("/record")
def record_consent(body: RecordConsentBody, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if body.choice not in ("consented", "declined"):
        raise HTTPException(400, "choice must be 'consented' or 'declined'")
    if body.source not in ("kiosk", "admin"):
        raise HTTPException(400, "source must be 'kiosk' or 'admin'")
    created = []
    for person_id in body.person_ids:
        if not session.get(Person, person_id):
            continue  # ignore unknown ids rather than failing the whole batch
        record = ConsentRecord(person_id=person_id, choice=body.choice, source=body.source)
        session.add(record)
        created.append(record)
    session.commit()
    return {"recorded": len(created)}


@router.get("/status")
def list_status(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    people = session.exec(select(Person)).all()
    latest = _latest_per_person(session)
    out = []
    for p in people:
        record = latest.get(p.id)
        out.append({
            "person_id": p.id,
            "participant_id": p.participant_id,
            "first_name": p.first_name,
            "last_name": p.last_name,
            "status": record.choice if record else "pending",
            "last_updated": record.recorded_at if record else None,
            # Where the current answer came from: "registration" (their sign-up
            # form), "kiosk" (they tapped it themselves) or "admin". Without
            # this the page cannot distinguish a form answer from a tap, which
            # matters for a record kept as compliance evidence.
            "source": record.source if record else None,
        })
    return out


@router.get("/status/{person_id}")
def person_status(person_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    p = session.get(Person, person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    history = session.exec(
        select(ConsentRecord).where(ConsentRecord.person_id == person_id).order_by(ConsentRecord.recorded_at.desc())
    ).all()
    return {
        "person_id": p.id,
        "participant_id": p.participant_id,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "status": history[0].choice if history else "pending",
        "last_updated": history[0].recorded_at if history else None,
        "history": [{"choice": h.choice, "source": h.source, "recorded_at": h.recorded_at} for h in history],
    }
