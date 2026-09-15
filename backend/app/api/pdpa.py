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
def list_status(page: int | None = None, page_size: int = 50, status: str | None = None, q: str | None = None,
                session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Phase I2 — with `page`, the Uploads envelope plus `counts` for the
    status tabs (always over everyone, whatever the filter). A participant's
    status is derived from their LATEST consent record, so the status filter
    and the slice are applied after that derivation rather than in SQL; the
    response itself is what pagination bounds. Without `page` the bare list is
    returned exactly as before."""
    people = session.exec(select(Person).order_by(Person.created_at, Person.id) if page is not None
                          else select(Person)).all()
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
    if page is None:
        return out

    from app.api.uploads import ALLOWED_PAGE_SIZES, DEFAULT_PAGE_SIZE

    page = max(1, int(page))
    page_size = page_size if page_size in ALLOWED_PAGE_SIZES else DEFAULT_PAGE_SIZE
    counts = {"all": len(out), "consented": 0, "declined": 0, "pending": 0}
    for row in out:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    if status in ("consented", "declined", "pending"):
        out = [r for r in out if r["status"] == status]
    if q and q.strip():
        needle = q.strip().lower()
        out = [r for r in out if needle in f"{r['first_name']} {r['last_name']}".lower()
               or needle in r["participant_id"].lower()]
    start = (page - 1) * page_size
    return {"items": out[start:start + page_size], "total": len(out), "page": page, "page_size": page_size,
            "counts": counts}


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
        # Additive: the participant page shows where the CURRENT answer came
        # from, the same way the list endpoint already does. A form answer is
        # not the same evidence as someone tapping the kiosk themselves.
        "source": history[0].source if history else None,
        "history": [{"choice": h.choice, "source": h.source, "recorded_at": h.recorded_at} for h in history],
    }
