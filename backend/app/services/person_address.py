"""Person.call_as — how a voice client should address a participant.

Kept out of the in-memory recognition index on purpose (that index is the
protected matching path); callers that have just matched someone read the
address with one indexed point lookup per request instead.
"""
from __future__ import annotations

import unicodedata
from typing import Iterable

from sqlmodel import Session, select

from app.models.models import Person

MAX_CALL_AS = 40


class CallAsError(ValueError):
    pass


def normalize_call_as(value: str | None) -> str | None:
    """Trim and collapse whitespace; empty -> None (use the name). Rejects
    control/format characters (newlines, zero-width marks) and anything over
    MAX_CALL_AS characters."""
    if value is None:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise CallAsError("Call-as may not contain control characters or line breaks.")
    cleaned = " ".join(value.split())
    if len(cleaned) > MAX_CALL_AS:
        raise CallAsError(f"Call-as is limited to {MAX_CALL_AS} characters.")
    return cleaned or None


def call_as_for(session: Session, person_ids: Iterable[str | None]) -> dict[str, str]:
    """{person_id: call_as} for the given ids that have one set."""
    ids = sorted({p for p in person_ids if p})
    if not ids:
        return {}
    rows = session.exec(select(Person.id, Person.call_as).where(Person.id.in_(ids))).all()
    return {pid: call_as for pid, call_as in rows if call_as}
