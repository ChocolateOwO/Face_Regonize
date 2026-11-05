"""Person.tags — short lowercase labels such as "vip" or "staff" (robot hub F1).

A tag is 1-32 characters after trimming and lowercasing: letters (any
script, including Thai vowel and tone marks), digits, "-", "_" or ".". No
spaces, commas or quotes. A person has at most MAX_TAGS tags, deduplicated, in
the order first given. Stored as a JSON list; an empty list means no tags.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Iterable

from sqlmodel import Session, select

from app.models.models import Person

MAX_TAGS = 20
MAX_TAG_LEN = 32
_EXTRA = set("-_.")


class TagError(ValueError):
    pass


def _clean(value: str) -> str | None:
    """One tag in canonical form, or None when it is not a valid tag."""
    tag = str(value).strip().lower()
    if not tag or len(tag) > MAX_TAG_LEN:
        return None
    for ch in tag:
        if not (ch.isalnum() or ch in _EXTRA or unicodedata.category(ch).startswith("M")):
            return None
    return tag


def split_text(text: str) -> list[str]:
    """A JSON array, or values separated by commas or semicolons."""
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            values = json.loads(text)
        except ValueError as exc:
            raise TagError("Tags must be a JSON list of strings or a comma-separated list.") from exc
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise TagError("Tags must be a JSON list of strings or a comma-separated list.")
        return values
    return [part for part in text.replace(";", ",").split(",")]


def sort_out(values: Iterable[str]) -> tuple[list[str], list[str]]:
    """(valid tags normalised + deduplicated, invalid raw values). Blank
    entries (e.g. "vip,,staff") are ignored, not invalid."""
    valid: list[str] = []
    invalid: list[str] = []
    for raw in values:
        if not str(raw).strip():
            continue
        tag = _clean(raw)
        if tag is None:
            invalid.append(str(raw).strip())
        elif tag not in valid:
            valid.append(tag)
    return valid, invalid


def normalize(values: Iterable[str]) -> list[str]:
    """Strict form for the API: any invalid value or too many tags -> TagError."""
    valid, invalid = sort_out(values)
    if invalid:
        raise TagError(f"Invalid tag(s): {', '.join(invalid[:5])}. A tag is 1-{MAX_TAG_LEN} letters, digits, '-', '_' or '.'.")
    if len(valid) > MAX_TAGS:
        raise TagError(f"A person can have at most {MAX_TAGS} tags.")
    return valid


def merge(current: Iterable[str] | None, new: Iterable[str]) -> list[str]:
    out = list(current or [])
    for tag in new:
        if tag not in out:
            out.append(tag)
    return out[:MAX_TAGS]


def tags_for(session: Session, person_ids: Iterable[str | None]) -> dict[str, list[str]]:
    """{person_id: tags} for the given ids (one indexed query)."""
    ids = sorted({p for p in person_ids if p})
    if not ids:
        return {}
    rows = session.exec(select(Person.id, Person.tags).where(Person.id.in_(ids))).all()
    return {pid: list(tags or []) for pid, tags in rows}
