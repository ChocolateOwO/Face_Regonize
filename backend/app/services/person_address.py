"""Person.call_as / title / language — how a voice client should address a
participant and which language to speak.

Kept out of the in-memory recognition index on purpose (that index is the
protected matching path); callers that have just matched someone read the
address with one indexed point lookup per request instead.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from sqlmodel import Session, select

from app.models.models import Person

MAX_CALL_AS = 40
MAX_TITLE = 40
MAX_LANGUAGE = 35
# BCP 47 shape: primary language (2-3 letters) plus optional subtags, e.g.
# "th", "en", "en-US", "zh-Hant-TW". Shape only — not a registry lookup.
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")


class CallAsError(ValueError):
    pass


def _clean_text(value: str | None, label: str, limit: int) -> str | None:
    if value is None:
        return None
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        raise CallAsError(f"{label} may not contain control characters or line breaks.")
    cleaned = " ".join(value.split())
    if len(cleaned) > limit:
        raise CallAsError(f"{label} is limited to {limit} characters.")
    return cleaned or None


def normalize_call_as(value: str | None) -> str | None:
    """Trim and collapse whitespace; empty -> None (use the name). Rejects
    control/format characters (newlines, zero-width marks) and anything over
    MAX_CALL_AS characters."""
    return _clean_text(value, "Call-as", MAX_CALL_AS)


def normalize_title(value: str | None) -> str | None:
    """Same rules as call_as; empty -> None (no title)."""
    return _clean_text(value, "Title", MAX_TITLE)


def normalize_language(value: str | None) -> str | None:
    """A BCP 47-shaped tag, canonical case (language lower, region upper,
    script title); empty -> None (not set)."""
    if value is None or not value.strip():
        return None
    tag = value.strip().replace("_", "-")
    if len(tag) > MAX_LANGUAGE or not _LANGUAGE_RE.match(tag):
        raise CallAsError("Language must be a language tag such as th, en or en-US.")
    parts = tag.split("-")
    out = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            out.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            out.append(part.title())
        else:
            out.append(part.lower())
    return "-".join(out)


def call_as_for(session: Session, person_ids: Iterable[str | None]) -> dict[str, str]:
    """{person_id: call_as} for the given ids that have one set."""
    ids = sorted({p for p in person_ids if p})
    if not ids:
        return {}
    rows = session.exec(select(Person.id, Person.call_as).where(Person.id.in_(ids))).all()
    return {pid: call_as for pid, call_as in rows if call_as}


def address_for(session: Session, person_ids: Iterable[str | None]) -> dict[str, dict]:
    """{person_id: {"call_as", "title", "language"}} (values may be None) for
    the given ids. One indexed query."""
    ids = sorted({p for p in person_ids if p})
    if not ids:
        return {}
    rows = session.exec(select(Person.id, Person.call_as, Person.title, Person.language)
                        .where(Person.id.in_(ids))).all()
    return {pid: {"call_as": call_as or None, "title": title or None, "language": language or None}
            for pid, call_as, title, language in rows}
