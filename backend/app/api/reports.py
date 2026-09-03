from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Attendance, FaceDetection, Person, Upload, User

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    people = session.exec(select(Person)).all()
    uploads = session.exec(select(Upload)).all()
    detections = session.exec(select(FaceDetection)).all()
    attendances = session.exec(select(Attendance)).all()

    today = date.today()
    detected_today_ids = {a.person_id for a in attendances if a.detected_at.date() == today}
    detected_ever_ids = {a.person_id for a in attendances}

    recent = sorted(detections, key=lambda d: d.detected_at, reverse=True)[:10]
    recent_out = []
    for d in recent:
        person = session.get(Person, d.person_id) if d.person_id else None
        upload = session.get(Upload, d.upload_id)
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
        "registered_total": len(people),
        "detected_total": len(detected_ever_ids),
        "detected_today": len(detected_today_ids),
        "uploaded_images_total": len(uploads),
        "recognition_attempts_total": len(detections),
        "unknown_faces_total": sum(1 for d in detections if d.status == "unknown"),
        "failed_uploads_total": sum(1 for u in uploads if u.processing_status == "failed"),
        "recent_activity": recent_out,
    }


@router.get("")
def reports(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    people = session.exec(select(Person)).all()
    attendances = session.exec(select(Attendance)).all()
    detections = session.exec(select(FaceDetection)).all()

    detected_ids = {a.person_id for a in attendances}
    attendance_pct = (len(detected_ids) / len(people) * 100) if people else 0.0

    counts = Counter(a.person_id for a in attendances)
    top = counts.most_common(10)
    top_out = []
    for person_id, count in top:
        p = session.get(Person, person_id)
        if p:
            top_out.append({"person_id": p.id, "name": f"{p.first_name} {p.last_name}".strip(), "count": count})

    timeline_counts: dict[str, int] = {}
    for d in detections:
        day = d.detected_at.date().isoformat()
        timeline_counts[day] = timeline_counts.get(day, 0) + 1
    timeline = [{"date": k, "count": v} for k, v in sorted(timeline_counts.items())]

    return {
        "total_participants": len(people),
        "total_detected_participants": len(detected_ids),
        "attendance_percentage": round(attendance_pct, 1),
        "total_detections": len(detections),
        "unknown_faces": sum(1 for d in detections if d.status == "unknown"),
        "most_frequent": top_out,
        "detection_timeline": timeline,
    }
