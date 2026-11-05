"""In-memory cache for settings read on the hot recognition path.

Loaded once at startup from the DB, updated in-place whenever Settings are
saved (settings_routes.py) — avoids a DB read on every recognition request.
"""
from __future__ import annotations

from app.config import DEFAULT_FACE_MATCH_THRESHOLD

_cache: dict[str, float | bool | str] = {
    "face_match_threshold": DEFAULT_FACE_MATCH_THRESHOLD,
    "debug_mode": False,
    # auto | cpu | gpu:N — Event Photo processing device (Phase G). "auto"
    # reuses the kiosk's warm FaceAnalysis singleton via the shared
    # inference_lock; an explicit device builds its own separate session.
    "event_processing_device": "auto",
    # Phase B — new batches use the staged/concurrent pipeline
    # (workflow_version=2) when True, the untouched sequential path
    # (workflow_version=1) when False. Defaults OFF: ships dark until
    # explicitly soaked and flipped, per the plan's own acceptance gate.
    "use_concurrent_pipeline": False,
}


def get_threshold() -> float:
    return float(_cache["face_match_threshold"])


def set_threshold(value: float) -> None:
    _cache["face_match_threshold"] = value


# Per-source match threshold overrides (robot hub F8): {source: threshold}.
# A source is a /api/look `source` label (as cleaned by look_service), a
# central camera node's node_id, or "kiosk". Anything not listed uses the
# global face_match_threshold.
THRESHOLD_OVERRIDES_KEY = "face_match_threshold_overrides"
THRESHOLD_MIN, THRESHOLD_MAX = 0.2, 0.8  # same range as the Settings page slider
MAX_OVERRIDES = 100
_threshold_overrides: dict[str, float] = {}


class OverrideError(ValueError):
    pass


def parse_threshold_overrides(text: str) -> dict[str, float]:
    """Validate the JSON object stored under THRESHOLD_OVERRIDES_KEY."""
    import json
    import re

    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        raise OverrideError("face_match_threshold_overrides must be a JSON object, e.g. {\"far-cam\": 0.4}.") from exc
    if not isinstance(data, dict):
        raise OverrideError("face_match_threshold_overrides must be a JSON object, e.g. {\"far-cam\": 0.4}.")
    if len(data) > MAX_OVERRIDES:
        raise OverrideError(f"At most {MAX_OVERRIDES} per-source thresholds.")
    out = {}
    for source, value in data.items():
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(source)):
            raise OverrideError(f"Source name {source!r} must be 1-64 letters, digits, '.', '_' or '-'.")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OverrideError(f"Threshold for {source!r} must be a number.")
        if not THRESHOLD_MIN <= float(value) <= THRESHOLD_MAX:
            raise OverrideError(f"Threshold for {source!r} must be between {THRESHOLD_MIN} and {THRESHOLD_MAX}.")
        out[str(source)] = round(float(value), 4)
    return out


def set_threshold_overrides(overrides: dict[str, float]) -> None:
    global _threshold_overrides
    _threshold_overrides = dict(overrides)  # replaced whole: readers never see a half-updated map


def get_threshold_overrides() -> dict[str, float]:
    return dict(_threshold_overrides)


def get_threshold_for(source: str | None) -> float:
    """The override for this source, else the global threshold."""
    if source and source in _threshold_overrides:
        return _threshold_overrides[source]
    return get_threshold()


def get_debug_mode() -> bool:
    return bool(_cache["debug_mode"])


def set_debug_mode(value: bool) -> None:
    _cache["debug_mode"] = value


def get_event_processing_device() -> str:
    return str(_cache["event_processing_device"])


def set_event_processing_device(value: str) -> None:
    _cache["event_processing_device"] = value


def get_use_concurrent_pipeline() -> bool:
    return bool(_cache["use_concurrent_pipeline"])


def set_use_concurrent_pipeline(value: bool) -> None:
    _cache["use_concurrent_pipeline"] = value


def load_from_db(session) -> None:
    from app.models.models import Setting

    threshold = session.get(Setting, "face_match_threshold")
    if threshold:
        _cache["face_match_threshold"] = float(threshold.value)
    debug = session.get(Setting, "debug_mode")
    if debug:
        _cache["debug_mode"] = debug.value.lower() == "true"
    event_device = session.get(Setting, "event_processing_device")
    if event_device:
        _cache["event_processing_device"] = event_device.value
    use_pipeline = session.get(Setting, "use_concurrent_pipeline")
    if use_pipeline:
        _cache["use_concurrent_pipeline"] = use_pipeline.value.lower() == "true"
    overrides = session.get(Setting, THRESHOLD_OVERRIDES_KEY)
    if overrides:
        try:
            set_threshold_overrides(parse_threshold_overrides(overrides.value))
        except OverrideError:
            # A stored value that no longer validates must not stop startup;
            # it is ignored (global threshold everywhere) until it is saved again.
            set_threshold_overrides({})
