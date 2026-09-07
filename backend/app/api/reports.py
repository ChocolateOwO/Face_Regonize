from __future__ import annotations

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Activity, Attendance, FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    today_start = datetime.combine(date.today(), time.min)
    registered_total = session.exec(select(func.count()).select_from(Person)).one()
    uploaded_images_total = session.exec(select(func.count()).select_from(Upload)).one()
    recognition_attempts_total = session.exec(select(func.count()).select_from(FaceDetection)).one()
    unknown_faces_total = session.exec(select(func.count()).select_from(FaceDetection).where(FaceDetection.status == "unknown")).one()
    failed_uploads_total = session.exec(select(func.count()).select_from(Upload).where(Upload.processing_status == "failed")).one()
    detected_total = session.exec(select(func.count(func.distinct(Attendance.person_id)))).one()
    detected_today = session.exec(select(func.count(func.distinct(Attendance.person_id))).where(Attendance.detected_at >= today_start)).one()
    recent = session.exec(
        select(FaceDetection, Person, Upload)
        .outerjoin(Person, FaceDetection.person_id == Person.id)
        .outerjoin(Upload, FaceDetection.upload_id == Upload.id)
        .order_by(FaceDetection.detected_at.desc())
        .limit(10)
    ).all()
    recent_out = []
    for d, person, upload in recent:
        recent_out.append({
            "person_name": f"{person.first_name} {person.last_name}".strip() if person else "Unknown",
            "person_image": person.image_path if person else None,
            # Version for person_image only - deliberately not called
            # updated_at, which in an activity record would read as "when this
            # detection changed" rather than "when the photo changed".
            "person_image_version": person.updated_at if person else None,
            "detected_at": d.detected_at,
            "confidence": d.confidence,
            "event_image": upload.image_path if upload else None,
            "status": d.status,
        })

    return {
        "registered_total": registered_total,
        "detected_total": detected_total,
        "detected_today": detected_today,
        "uploaded_images_total": uploaded_images_total,
        "recognition_attempts_total": recognition_attempts_total,
        "unknown_faces_total": unknown_faces_total,
        "failed_uploads_total": failed_uploads_total,
        "recent_activity": recent_out,
    }


@router.get("")
def reports(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    total_people = session.exec(select(func.count()).select_from(Person)).one()
    detected_people = session.exec(select(func.count(func.distinct(Attendance.person_id)))).one()
    total_detections = session.exec(select(func.count()).select_from(FaceDetection)).one()
    unknown_faces = session.exec(select(func.count()).select_from(FaceDetection).where(FaceDetection.status == "unknown")).one()
    top_rows = session.exec(
        select(Person.id, Person.first_name, Person.last_name, func.count(Attendance.id).label("count"))
        .join(Attendance, Attendance.person_id == Person.id)
        .group_by(Person.id, Person.first_name, Person.last_name)
        .order_by(func.count(Attendance.id).desc())
        .limit(10)
    ).all()
    top_out = [{"person_id": person_id, "name": f"{first_name} {last_name}".strip(), "count": count} for person_id, first_name, last_name, count in top_rows]
    timeline_rows = session.exec(
        select(func.date(FaceDetection.detected_at), func.count(FaceDetection.id))
        .group_by(func.date(FaceDetection.detected_at))
        .order_by(func.date(FaceDetection.detected_at))
    ).all()
    timeline = [{"date": day, "count": count} for day, count in timeline_rows]

    return {
        "total_participants": total_people,
        "total_detected_participants": detected_people,
        "attendance_percentage": round(detected_people / total_people * 100, 1) if total_people else 0.0,
        "total_detections": total_detections,
        "unknown_faces": unknown_faces,
        "most_frequent": top_out,
        "detection_timeline": timeline,
    }


# The "activity" a check-in belongs to is nullable, and a NULL is not a gap in
# the data - it is a real category: someone checked in while no activity was
# selected, or before activities existed at all. It gets its own bucket rather
# than being dropped, so the per-activity numbers always add up to the total.
NO_ACTIVITY_ID = ""
NO_ACTIVITY_NAME = "No activity"


def _activity_rows(session: Session) -> list[dict]:
    """One row per activity, plus the unassigned bucket. Ordered by check-ins."""
    activities = session.exec(select(Activity)).all()
    total_participants = session.exec(select(func.count()).select_from(Person)).one()
    aggregate_rows = session.exec(
        select(Attendance.activity_id, func.count(Attendance.id), func.count(func.distinct(Attendance.person_id)), func.min(Attendance.detected_at), func.max(Attendance.detected_at)).group_by(Attendance.activity_id)
    ).all()
    by_activity = {activity_id or NO_ACTIVITY_ID: values for activity_id, *values in aggregate_rows}

    rows: list[dict] = []
    for act in activities:
        rows.append(_summarise(act.id, act.name, act.archived, by_activity.get(act.id), total_participants))

    unassigned = by_activity.get(NO_ACTIVITY_ID)
    if unassigned:
        # Only shown when it actually contains something - an event that always
        # used activities should not see an empty "No activity" row forever.
        rows.append(_summarise(NO_ACTIVITY_ID, NO_ACTIVITY_NAME, False,
                               unassigned, total_participants))

    rows.sort(key=lambda r: (-r["checked_in"], r["name"]))
    return rows


def _summarise(activity_id: str, name: str, archived: bool,
               values: list | None, total_participants: int) -> dict:
    """check_ins counts every record; checked_in counts distinct PEOPLE.

    They differ whenever someone is seen more than once at the same station,
    and conflating them would overstate turnout - so both are reported.
    """
    check_ins, checked_in, first_check_in, last_check_in = values or (0, 0, None, None)
    return {
        "activity_id": activity_id,
        "name": name,
        "archived": archived,
        "checked_in": checked_in,
        "check_ins": check_ins,
        "attendance_percentage": round(checked_in / total_participants * 100, 1) if total_participants else 0.0,
        "first_check_in": first_check_in.isoformat() if isinstance(first_check_in, datetime) else first_check_in,
        "last_check_in": last_check_in.isoformat() if isinstance(last_check_in, datetime) else last_check_in,
    }


@router.get("/activities")
def reports_by_activity(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Turnout for every activity, including archived ones.

    Archived activities are deliberately still reported: archiving only hides an
    activity from the kiosk picker, and its history still happened.
    """
    rows = _activity_rows(session)
    return {
        "total_participants": session.exec(select(func.count()).select_from(Person)).one(),
        "total_check_ins": session.exec(select(func.count()).select_from(Attendance)).one(),
        "activities": rows,
    }


@router.get("/activities/{activity_id}")
def report_for_activity(activity_id: str, session: Session = Depends(get_session),
                        user: User = Depends(get_current_user)):
    """Who checked in to one activity, most recent first.

    `activity_id` may be "none" for the unassigned bucket, which cannot be
    addressed by a real id because its check-ins have activity_id NULL.
    """
    if activity_id == "none":
        name, archived = NO_ACTIVITY_NAME, False
        records = session.exec(
            select(Attendance).where(Attendance.activity_id.is_(None))
            .order_by(Attendance.detected_at.desc())
        ).all()
    else:
        activity = session.get(Activity, activity_id)
        if not activity:
            raise HTTPException(404, "Activity not found")
        name, archived = activity.name, activity.archived
        records = session.exec(
            select(Attendance).where(Attendance.activity_id == activity_id)
            .order_by(Attendance.detected_at.desc())
        ).all()

    # One row per PERSON, not per check-in: the question this page answers is
    # "who came", with how many times they were seen as a detail.
    seen: dict[str, dict] = {}
    for a in records:
        entry = seen.get(a.person_id)
        if entry is None:
            p = session.get(Person, a.person_id)
            if not p:
                continue          # participant deleted since; skip rather than show a blank row
            seen[a.person_id] = {
                "person_id": p.id,
                "participant_id": p.participant_id,
                "name": f"{p.first_name} {p.last_name}".strip(),
                "check_ins": 1,
                "first_check_in": a.detected_at.isoformat(),
                "last_check_in": a.detected_at.isoformat(),
            }
        else:
            entry["check_ins"] += 1
            # records arrive newest-first, so each later row is an earlier time
            entry["first_check_in"] = a.detected_at.isoformat()

    return {
        "activity_id": activity_id,
        "name": name,
        "archived": archived,
        "checked_in": len(seen),
        "check_ins": len(records),
        "participants": list(seen.values()),
    }
