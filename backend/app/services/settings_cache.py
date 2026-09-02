"""In-memory cache for settings read on the hot recognition path.

Loaded once at startup from the DB, updated in-place whenever Settings are
saved (settings_routes.py) — avoids a DB read on every recognition request.
"""
from __future__ import annotations

from app.config import DEFAULT_FACE_MATCH_THRESHOLD

_cache: dict[str, float | bool] = {
    "face_match_threshold": DEFAULT_FACE_MATCH_THRESHOLD,
    "debug_mode": False,
}


def get_threshold() -> float:
    return float(_cache["face_match_threshold"])


def set_threshold(value: float) -> None:
    _cache["face_match_threshold"] = value


def get_debug_mode() -> bool:
    return bool(_cache["debug_mode"])


def set_debug_mode(value: bool) -> None:
    _cache["debug_mode"] = value


def load_from_db(session) -> None:
    from app.models.models import Setting

    threshold = session.get(Setting, "face_match_threshold")
    if threshold:
        _cache["face_match_threshold"] = float(threshold.value)
    debug = session.get(Setting, "debug_mode")
    if debug:
        _cache["debug_mode"] = debug.value.lower() == "true"
