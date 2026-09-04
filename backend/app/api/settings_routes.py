from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.config import (
    DEFAULT_FACE_DETECTION_CONFIDENCE,
    DEFAULT_FACE_MATCH_THRESHOLD,
    STORAGE_PATH,
)
from app.database.db import get_session
from app.models.models import Setting, User
from app.services import settings_cache

router = APIRouter(prefix="/api/settings", tags=["settings"])

DEFAULTS = {
    "face_match_threshold": str(DEFAULT_FACE_MATCH_THRESHOLD),
    "face_detection_confidence": str(DEFAULT_FACE_DETECTION_CONFIDENCE),
    "event_name": "My Conference",
    "event_date": "",
    "event_location": "",
    "app_name": "Reconize",
    "timezone": "UTC",
    "debug_mode": "false",
    # App-wide default camera, stored as the device LABEL rather than the
    # browser deviceId: a deviceId is scoped to one browser profile and
    # origin, so it would be meaningless on any other machine. Empty means
    # "let the browser choose", which is the original behaviour.
    "camera_label": "",
    # "tap"    - show TAP TO SCAN, ask PDPA consent, then scan (original behaviour)
    # "always" - camera runs continuously, no prompt, recognised people are
    #            checked in as they are seen. Consent for this mode comes from
    #            the registration form, not the kiosk.
    "kiosk_mode": "tap",
}


class SettingsUpdate(BaseModel):
    values: dict[str, str]


@router.get("")
def get_settings(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    stored = {s.key: s.value for s in session.exec(select(Setting)).all()}
    merged = {**DEFAULTS, **stored}
    merged["storage_path"] = str(STORAGE_PATH)
    return merged


@router.put("")
def update_settings(body: SettingsUpdate, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    for key, value in body.values.items():
        if key not in DEFAULTS:
            continue
        setting = session.get(Setting, key)
        if setting:
            setting.value = value
        else:
            setting = Setting(key=key, value=value)
        session.add(setting)
    session.commit()

    # Recognition reads these from memory, not the DB, so push changes into
    # the cache immediately — no restart required for a threshold tweak to
    # take effect on the very next scan.
    if "face_match_threshold" in body.values:
        settings_cache.set_threshold(float(body.values["face_match_threshold"]))
    if "debug_mode" in body.values:
        settings_cache.set_debug_mode(body.values["debug_mode"].lower() == "true")

    stored = {s.key: s.value for s in session.exec(select(Setting)).all()}
    merged = {**DEFAULTS, **stored}
    merged["storage_path"] = str(STORAGE_PATH)
    return merged
