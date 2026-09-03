from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_current_user
from app.database.db import get_session
from app.face_recognition.engine import detect_faces
from app.face_recognition.index import recognition_index
from app.services.index_sync import apply_index_change
from app.models.models import Attendance, ConsentRecord, FaceDetection, Person, User
from app.services import storage_service

router = APIRouter(prefix="/api/people", tags=["people"])


def _person_out(session: Session, p: Person) -> dict:
    detections = session.exec(select(Attendance).where(Attendance.person_id == p.id)).all()
    times = sorted(d.detected_at for d in detections)
    return {
        "id": p.id,
        "participant_id": p.participant_id,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "email": p.email,
        "image_path": p.image_path,
        "image_source": p.image_source,
        "original_image_url": p.original_image_url,
        "det_score": p.det_score,
        "is_demo": p.is_demo,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "detection_count": len(detections),
        "first_detected": times[0] if times else None,
        "last_detected": times[-1] if times else None,
    }


@router.get("")
def list_people(q: str | None = None, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    stmt = select(Person)
    people = session.exec(stmt).all()
    if q:
        ql = q.lower()
        people = [
            p for p in people
            if ql in p.first_name.lower() or ql in p.last_name.lower()
            or ql in p.participant_id.lower() or (p.email and ql in p.email.lower())
        ]
    return [_person_out(session, p) for p in people]


@router.get("/{person_id}")
def get_person(person_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    p = session.get(Person, person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    out = _person_out(session, p)

    history = session.exec(
        select(Attendance).where(Attendance.person_id == person_id).order_by(Attendance.detected_at.desc())
    ).all()
    out["attendance_history"] = [
        {
            "id": h.id,
            "upload_id": h.upload_id,
            "confidence": h.confidence,
            "detected_at": h.detected_at,
        }
        for h in history
    ]
    return out


@router.post("")
def create_person(
    participant_id: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(""),
    email: str | None = Form(None),
    photo: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    existing = session.exec(select(Person).where(Person.participant_id == participant_id)).first()
    if existing:
        raise HTTPException(409, f"Participant ID '{participant_id}' already exists.")

    file_bytes = photo.file.read()
    img = storage_service.decode_image(file_bytes)
    if img is None:
        raise HTTPException(400, "Could not decode the uploaded image.")

    faces = detect_faces(img)
    if not faces:
        raise HTTPException(422, "No face detected. Please upload a clear face image.")
    if len(faces) > 1:
        raise HTTPException(422, "Multiple faces detected. Please upload an image containing only one person.")
    face = faces[0]

    image_path = storage_service.save_person_image(participant_id, file_bytes)

    person = Person(
        participant_id=participant_id,
        first_name=first_name,
        last_name=last_name,
        email=email or None,
        image_path=image_path,
        image_source="manual",
        embedding=face.embedding.tobytes(),
        det_score=face.det_score,
    )
    session.add(person)
    session.commit()
    session.refresh(person)
    # DB is already committed; if the index update fails, rebuild it from the
    # committed rows so RAM cannot silently disagree with the database.
    apply_index_change(session, lambda: recognition_index.upsert(person), what="create participant")
    return _person_out(session, person)


@router.put("/{person_id}")
def update_person(
    person_id: str,
    first_name: str = Form(...),
    last_name: str = Form(""),
    email: str | None = Form(None),
    photo: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    p = session.get(Person, person_id)
    if not p:
        raise HTTPException(404, "Person not found")

    p.first_name = first_name
    p.last_name = last_name
    p.email = email or None
    p.updated_at = datetime.now()

    if photo is not None:
        file_bytes = photo.file.read()
        img = storage_service.decode_image(file_bytes)
        if img is None:
            raise HTTPException(400, "Could not decode the uploaded image.")
        faces = detect_faces(img)
        if not faces:
            raise HTTPException(422, "No face detected. Please upload a clear face image.")
        if len(faces) > 1:
            raise HTTPException(422, "Multiple faces detected. Please upload an image containing only one person.")
        face = faces[0]
        p.image_path = storage_service.save_person_image(p.participant_id, file_bytes)
        p.embedding = face.embedding.tobytes()
        p.det_score = face.det_score
        p.image_source = "manual"

    session.add(p)
    session.commit()
    session.refresh(p)
    apply_index_change(session, lambda: recognition_index.upsert(p), what="update participant")
    return _person_out(session, p)


@router.delete("")
def delete_all_people(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Full wipe: deletes every Person plus everything that references
    them — Attendance, PDPA ConsentRecord, and any FaceDetection tied to a
    person (unknown-face detections with no person_id are left alone, since
    they don't reference anyone). Also clears the in-memory recognition
    index so it stops matching against embeddings that no longer exist."""
    people = session.exec(select(Person)).all()
    count = len(people)

    for a in session.exec(select(Attendance)).all():
        session.delete(a)
    for c in session.exec(select(ConsentRecord)).all():
        session.delete(c)
    for d in session.exec(select(FaceDetection)).all():
        if d.person_id:
            session.delete(d)
    for p in people:
        session.delete(p)
    session.commit()

    apply_index_change(session, lambda: recognition_index.rebuild([]), what="delete all participants")
    return {"deleted": count}


@router.delete("/{person_id}")
def delete_person(person_id: str, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    p = session.get(Person, person_id)
    if not p:
        raise HTTPException(404, "Person not found")
    session.exec(select(FaceDetection).where(FaceDetection.person_id == person_id))
    for a in session.exec(select(Attendance).where(Attendance.person_id == person_id)).all():
        session.delete(a)
    session.delete(p)
    session.commit()
    apply_index_change(session, lambda: recognition_index.remove(person_id), what="delete participant")
    return {"deleted": True}
