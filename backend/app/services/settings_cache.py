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
