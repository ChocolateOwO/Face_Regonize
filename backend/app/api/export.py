from __future__ import annotations

import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Attendance, FaceDetection, Person, Upload, User
from app.services.export_service import to_csv_bytes, to_xlsx_bytes

router = APIRouter(prefix="/api/export", tags=["export"])


def _respond(rows: list[dict], fmt: str, base_filename: str):
    if fmt == "xlsx":
        data = to_xlsx_bytes(rows)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        name = f"{base_filename}.xlsx"
    else:
        data = to_csv_bytes(rows)
        media = "text/csv"
        name = f"{base_filename}.csv"
    return StreamingResponse(io.BytesIO(data), media_type=media, headers={"Content-Disposition": f"attachment; filename={name}"})


@router.get("/people")
def export_people(format: str = "csv", session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    people = session.exec(select(Person)).all()
    rows = []
    for p in people:
        detections = session.exec(select(Attendance).where(Attendance.person_id == p.id)).all()
        times = sorted(d.detected_at for d in detections)
        rows.append({
            "Participant ID": p.participant_id,
            "First Name": p.first_name,
            "Last Name": p.last_name,
            "Email": p.email or "",
            "Registration Date": p.created_at.isoformat(),
            "First Detected": times[0].isoformat() if times else "",
            "Last Detected": times[-1].isoformat() if times else "",
            "Detections": len(detections),
        })
    return _respond(rows, format, "participants")


@router.get("/attendance")
def export_attendance(format: str = "csv", session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    records = session.exec(select(Attendance).order_by(Attendance.detected_at.desc())).all()
    rows = []
    for a in records:
        p = session.get(Person, a.person_id)
        rows.append({
            "Participant ID": p.participant_id if p else "",
            "Name": f"{p.first_name} {p.last_name}".strip() if p else "",
            "Date": a.detected_at.date().isoformat(),
            "Time": a.detected_at.time().isoformat(timespec="seconds"),
            "Confidence": round(a.confidence, 3),
            "Upload ID": a.upload_id,
        })
    return _respond(rows, format, "attendance")


@router.get("/history")
def export_history(format: str = "csv", session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if format not in ("csv", "xlsx"):
        raise HTTPException(400, "format must be csv or xlsx")
    detections = session.exec(select(FaceDetection).order_by(FaceDetection.detected_at.desc())).all()
    rows = []
    for d in detections:
        p = session.get(Person, d.person_id) if d.person_id else None
        u = session.get(Upload, d.upload_id)
        rows.append({
            "Date": d.detected_at.date().isoformat(),
            "Time": d.detected_at.time().isoformat(timespec="seconds"),
            "Person": f"{p.first_name} {p.last_name}".strip() if p else "Unknown",
            "Confidence": round(d.confidence, 3),
            "Status": d.status,
            "Source Image": u.filename if u else "",
        })
    return _respond(rows, format, "recognition_history")
