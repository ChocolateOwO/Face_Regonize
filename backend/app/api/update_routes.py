"""Application update endpoints. Admin-only, using the same auth dependency
as every other route in this app — a kiosk user can never reach these.

No endpoint here ever returns the GitHub token, its length, or any string
derived from it; `github_configured` is a bare boolean, and every error that
originates from a subprocess is passed through update_service.redact() first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.config import APP_VERSION
from app.database.db import get_session
from app.models.models import UpdateJob, User
from app.services import maintenance, update_service

router = APIRouter(prefix="/api/update", tags=["update"])


class InstallBody(BaseModel):
    tag: str | None = None


@router.get("/version")
def get_version():
    """Unauthenticated on purpose and deliberately minimal: the updater polls
    this from outside the app to confirm which version came back up after a
    restart. It exposes nothing that the login page does not already show."""
    return {"version": APP_VERSION}


@router.get("/status")
def get_status(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    info = update_service.check_for_update(force=False)
    abandoned = maintenance.abandoned_update()
    history = session.exec(select(UpdateJob).order_by(UpdateJob.finished_at.desc()).limit(5)).all()
    return {
        **info,
        "maintenance_active": maintenance.is_active(),
        "maintenance_reason": maintenance.reason(),
        "abandoned_update": bool(abandoned),
        "abandoned_log_path": (abandoned or {}).get("log_path", "") if abandoned else "",
        "history": [
            {
                "from_version": job.from_version,
                "to_version": job.to_version,
                "status": job.status,
                "error": job.error,
                "finished_at": job.finished_at,
            }
            for job in history
        ],
    }


@router.post("/check")
def force_check(user: User = Depends(get_current_user)):
    return update_service.check_for_update(force=True)


@router.post("/preflight")
def run_preflight(body: InstallBody, user: User = Depends(get_current_user)):
    """Shows the admin exactly why an update would be refused, without
    starting one."""
    try:
        return update_service.preflight(body.tag)
    except update_service.UpdateError as e:
        raise HTTPException(400, str(e))


@router.post("/install")
def install(body: InstallBody, user: User = Depends(get_current_user)):
    """Never runs automatically — reachable only by an authenticated admin
    explicitly asking for it."""
    try:
        return update_service.start_update(body.tag)
    except update_service.UpdateError as e:
        raise HTTPException(400, str(e))


@router.post("/rollback")
def rollback(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Deliberate return to the previously installed version, reusing the same
    machinery as a normal update (backup, integrity check, verified restart)
    rather than a second, less-tested code path. Automatic rollback after a
    failed update is handled by the updater itself; this is the manual door.

    Requires that version to still exist as a published release."""
    last = session.exec(
        select(UpdateJob).where(UpdateJob.status == "completed").order_by(UpdateJob.finished_at.desc()).limit(1)
    ).first()
    if not last:
        raise HTTPException(400, "No previous completed update to roll back to.")
    if last.from_version == APP_VERSION:
        raise HTTPException(400, f"Already running {APP_VERSION}.")
    try:
        return update_service.start_update(f"v{last.from_version}", allow_downgrade=True)
    except update_service.UpdateError as e:
        raise HTTPException(400, str(e))


@router.get("/progress")
def progress(user: User = Depends(get_current_user)):
    return update_service.progress()
