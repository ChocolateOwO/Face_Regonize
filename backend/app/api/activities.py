"""Event activities, and which one the kiosk is currently checking people into.

An "activity" is a station within an event - Registration, Food, Gadget. The
same participant can be checked in once to each, which is what makes this
different from the single check-in-per-day the kiosk had before.

The current activity is kept in the Setting table under `current_activity_id`
rather than as a flag on Activity, so "exactly one is current" holds by
construction instead of needing every other row to be cleared on each change.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Activity, Attendance, Setting, User

router = APIRouter(prefix="/api/activities", tags=["activities"])

CURRENT_KEY = "current_activity_id"


class ActivityCreate(BaseModel):
    name: str


class ActivityUpdate(BaseModel):
    name: str | None = None
    archived: bool | None = None


class CurrentUpdate(BaseModel):
    # "" clears the selection, putting the kiosk back to plain attendance.
    activity_id: str


def get_current_activity_id(session: Session) -> str:
    row = session.get(Setting, CURRENT_KEY)
    return row.value if row else ""


def _set_current(session: Session, activity_id: str) -> None:
    row = session.get(Setting, CURRENT_KEY)
    if row:
        row.value = activity_id
    else:
        row = Setting(key=CURRENT_KEY, value=activity_id)
    session.add(row)


def _out(session: Session, a: Activity, current_id: str) -> dict:
    checked_in = len(session.exec(select(Attendance).where(Attendance.activity_id == a.id)).all())
    return {
        "id": a.id,
        "name": a.name,
        "archived": a.archived,
        "created_at": a.created_at,
        "is_current": a.id == current_id,
        "checked_in_count": checked_in,
    }


@router.get("")
def list_activities(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    current_id = get_current_activity_id(session)
    rows = session.exec(select(Activity).order_by(Activity.created_at)).all()
    return {
        "current_activity_id": current_id,
        "activities": [_out(session, a, current_id) for a in rows],
    }


@router.post("")
def create_activity(
    body: ActivityCreate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "An activity needs a name.")
    existing = session.exec(select(Activity).where(Activity.name == name)).first()
    if existing:
        raise HTTPException(409, f"An activity called '{name}' already exists.")

    activity = Activity(name=name)
    session.add(activity)
    session.commit()
    session.refresh(activity)

    # The first activity created becomes current, so a fresh setup does not
    # silently record check-ins against no activity at all.
    if not get_current_activity_id(session):
        _set_current(session, activity.id)
        session.commit()

    return _out(session, activity, get_current_activity_id(session))


@router.put("/current")
def set_current_activity(
    body: CurrentUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if body.activity_id:
        activity = session.get(Activity, body.activity_id)
        if not activity:
            raise HTTPException(404, "Activity not found")
        if activity.archived:
            raise HTTPException(400, "That activity is archived. Restore it before making it current.")
    _set_current(session, body.activity_id)
    session.commit()
    return {"current_activity_id": body.activity_id}


@router.put("/{activity_id}")
def update_activity(
    activity_id: str,
    body: ActivityUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    activity = session.get(Activity, activity_id)
    if not activity:
        raise HTTPException(404, "Activity not found")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "An activity needs a name.")
        clash = session.exec(
            select(Activity).where(Activity.name == name, Activity.id != activity_id)
        ).first()
        if clash:
            raise HTTPException(409, f"An activity called '{name}' already exists.")
        activity.name = name

    if body.archived is not None:
        activity.archived = body.archived
        # An archived activity must not stay selected, or the kiosk would keep
        # checking people into something hidden from its own picker.
        if body.archived and get_current_activity_id(session) == activity_id:
            _set_current(session, "")

    session.add(activity)
    session.commit()
    session.refresh(activity)
    return _out(session, activity, get_current_activity_id(session))


@router.delete("/{activity_id}")
def delete_activity(
    activity_id: str,
    delete_attendance: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Delete an activity. What happens to its check-ins is the caller's choice.

    Those rows are the event's record of who was there, so the destructive
    option is never the default:

      delete_attendance=false (default)
          the check-ins are KEPT and detached - their activity_id is cleared,
          so they still count as attendance and appear under "No activity" in
          reports. Nothing about who was present is lost.

      delete_attendance=true
          the check-ins are deleted along with the activity. Irreversible, and
          intended for clearing out test activities.

    Defaulting to false matters beyond the UI: anything calling this endpoint
    without thinking about the flag gets the option that cannot lose data.
    """
    activity = session.get(Activity, activity_id)
    if not activity:
        raise HTTPException(404, "Activity not found")

    records = session.exec(select(Attendance).where(Attendance.activity_id == activity_id)).all()

    if delete_attendance:
        for record in records:
            session.delete(record)
    else:
        # Detach rather than cascade. A row whose activity_id still pointed at a
        # deleted activity would be a dangling reference, and reports would show
        # a blank name instead of an honest "No activity".
        for record in records:
            record.activity_id = None
            session.add(record)

    if get_current_activity_id(session) == activity_id:
        _set_current(session, "")
    session.delete(activity)
    session.commit()
    return {
        "deleted": True,
        "attendance_deleted": len(records) if delete_attendance else 0,
        "attendance_kept": 0 if delete_attendance else len(records),
    }
