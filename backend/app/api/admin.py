from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.models.models import Attendance, FaceDetection, Upload, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/data-counts")
def data_counts(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Counts shown in the admin confirmation dialogs before a destructive
    clear action — recognition_history_count matches what GET /api/history
    lists (one row per FaceDetection), since that's what "records" means to
    an admin reading the History page."""
    return {
        "attendance_count": len(session.exec(select(Attendance.id)).all()),
        "recognition_history_count": len(session.exec(select(FaceDetection.id)).all()),
        "upload_count": len(session.exec(select(Upload.id)).all()),
    }


def _clear_attendance(session: Session) -> int:
    rows = session.exec(select(Attendance)).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def _clear_recognition_history(session: Session) -> int:
    """Deletes FaceDetection (recognition/scan history) and Upload (upload
    history) rows. Does not touch Attendance, Person, embeddings, or the
    in-memory recognition_index. Any surviving Attendance.face_detection_id /
    upload_id becomes a dangling reference, which is safe here: SQLite FK
    enforcement is off in this app, and no endpoint ever dereferences those
    two Attendance fields (export_attendance only writes upload_id out as a
    plain string, never looks it up)."""
    detections = session.exec(select(FaceDetection)).all()
    uploads = session.exec(select(Upload)).all()
    for d in detections:
        session.delete(d)
    for u in uploads:
        session.delete(u)
    session.commit()
    return len(detections)


@router.post("/clear-attendance")
def clear_attendance(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return {"deleted": _clear_attendance(session)}


@router.post("/clear-recognition-history")
def clear_recognition_history(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return {"deleted": _clear_recognition_history(session)}


@router.post("/clear-all-event-data")
def clear_all_event_data(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Attendance is cleared before recognition history so nothing in this
    request ever points at an already-deleted row, even transiently."""
    attendance_deleted = _clear_attendance(session)
    history_deleted = _clear_recognition_history(session)
    return {"attendance_deleted": attendance_deleted, "history_deleted": history_deleted}
