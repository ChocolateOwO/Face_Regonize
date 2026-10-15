from __future__ import annotations

import logging
import time
from datetime import date, datetime

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_api_client, is_machine
from app.database.db import engine, get_session
from app.face_recognition.engine import bbox_to_str, detect_faces, get_active_provider, resize_for_detection, MODEL_NAME
from app.face_recognition.index import recognition_index
from app.api.activities import get_current_activity_id
from app.models.models import Activity, Attendance, FaceDetection, Upload, User
from app.services import events_feed, settings_cache, storage_service
from app.services.person_address import call_as_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recognition", tags=["recognition"])


def _start_of_today() -> datetime:
    return datetime.combine(date.today(), datetime.min.time())


def _checked_in_today(session: Session, person_id: str, activity_id: str = "") -> bool:
    """Already-checked-in is scoped to the current calendar day AND to the
    activity being checked into - one Attendance row per person per activity
    per day. That is what lets the same person collect food and then collect a
    gadget without the second scan being dismissed as a duplicate, while still
    letting the kiosk be reused on a later date without wiping the DB.

    With no activity selected the activity clause drops out and the behaviour
    is exactly the original one check-in per person per day.

    Attendance.person_id and .activity_id are both indexed, so this stays a
    cheap point lookup."""
    stmt = select(Attendance.id).where(
        Attendance.person_id == person_id,
        Attendance.detected_at >= _start_of_today(),
    )
    if activity_id:
        stmt = stmt.where(Attendance.activity_id == activity_id)
    return session.exec(stmt.limit(1)).first() is not None


def _resolve_activity(session: Session, requested: str | None) -> str:
    """A station-supplied activity wins over the app-wide current one.

    An id that does not exist is rejected rather than silently ignored: a
    station pinned to a deleted activity must not quietly start recording
    against whatever the global setting happens to be, because the operator
    would have no way of noticing.
    """
    if requested:
        activity = session.get(Activity, requested)
        if not activity:
            raise HTTPException(404, "That activity no longer exists. Reopen this station from the Activities page.")
        if activity.archived:
            raise HTTPException(400, f"'{activity.name}' is archived and cannot take check-ins.")
        return activity.id
    return get_current_activity_id(session)


def _persist_recognition(file_bytes: bytes, filename: str, user_id: str, threshold: float, faces, matches, scaled_bboxes, new_person_ids: set[str], activity_id: str = "", feed_node_id: str = "kiosk", source_kind: str = "kiosk") -> None:
    """Runs AFTER the recognition response has already been sent to the
    client — image storage, thumbnailing, and every DB write happen here so
    none of it adds latency to the identity result. Opens its own session
    since the request-scoped one is gone by the time this runs.

    Only person_ids in `new_person_ids` (people who were NOT already checked
    in today as of the synchronous request handler) get an Attendance row —
    everyone else still gets a FaceDetection row (full scan history is kept),
    just not a second Attendance row, so a person lingering in frame across
    many scan ticks doesn't accumulate duplicate check-ins."""
    t0 = time.perf_counter()
    image_path, _thumb = storage_service.save_event_image(file_bytes, filename)
    t_save = time.perf_counter()
    matched_count = sum(1 for m in matches if m[0])
    unknown_count = len(matches) - matched_count

    with Session(engine) as session:
        upload = Upload(
            filename=filename,
            image_path=image_path,
            uploaded_by=user_id,
            processing_status="completed",
            threshold_used=threshold,
            faces_total=len(faces),
            faces_matched=matched_count,
            faces_unknown=unknown_count,
            source_kind=source_kind,
            source_node_id=feed_node_id,
        )
        session.add(upload)
        session.commit()
        session.refresh(upload)
        addresses = call_as_for(session, (m[0] for m in matches))

        for face, (person_id, _first, full_name, participant_id, score), bbox in zip(faces, matches, scaled_bboxes):
            status = "matched" if person_id else "unknown"
            detection = FaceDetection(
                upload_id=upload.id,
                person_id=person_id,
                confidence=score,
                bbox=bbox_to_str(bbox),
                status=status,
            )
            session.add(detection)
            session.commit()
            session.refresh(detection)

            checkin = "already"
            if person_id and person_id in new_person_ids:
                # Defense-in-depth re-check: the synchronous handler decided
                # this person was "new" before this background task ran, but
                # a near-simultaneous second scan of the same person could
                # theoretically race it. Re-checking here (instead of trusting
                # the earlier decision blindly) is what actually guarantees no
                # duplicate Attendance row, not just the happy-path timing.
                if not _checked_in_today(session, person_id, activity_id):
                    session.add(Attendance(
                        person_id=person_id,
                        upload_id=upload.id,
                        face_detection_id=detection.id,
                        confidence=score,
                        activity_id=activity_id or None,
                    ))
                    session.commit()
                    checkin = "new"

            if person_id:
                # Same live-events feed the CCTV/mobile nodes already publish
                # to (api/nodes.py). This is the ONLY publish for a frame that
                # went through run_recognition(): the kiosk's /upload passes
                # the constant "kiosk" tag (the kiosk never registers itself
                # as a node), /api/node/recognize-central passes its real
                # node_id. Publishing here, after the write, means `checkin`
                # describes the Attendance row that actually exists.
                events_feed.publish({
                    "node_id": feed_node_id,
                    "participant_id": participant_id,
                    "name": full_name,
                    "activity_id": activity_id or None,
                    "checkin": checkin,
                    "at": datetime.now().isoformat(),
                    "call_as": addresses.get(person_id),
                })
    t_db = time.perf_counter()
    logger.info(
        "background persist: image_save=%.0fms db_write=%.0fms (both async — did not add to response latency)",
        (t_save - t0) * 1000,
        (t_db - t_save) * 1000,
    )


def run_recognition(
    file_bytes: bytes,
    filename: str,
    *,
    background_tasks: BackgroundTasks,
    session: Session,
    user_id: str,
    activity_id: str | None,
    debug: bool | None,
    persist: bool = True,
    feed_node_id: str = "kiosk",
    source_kind: str = "kiosk",
    observed: dict | None = None,
) -> dict:
    """The recognize-and-checkin core, shared by every entry point that hands
    Central a frame to identify: the kiosk's own /upload route below, AND
    the distributed-node mobile/central-inference endpoint (api/nodes.py).

    Extracted rather than duplicated so there is exactly one place that
    decides "who is this and have they already checked in" - a second,
    independently-maintained copy is exactly how the two paths would
    eventually disagree. /upload's behaviour and response shape are
    byte-for-byte unchanged by this extraction; it is a straight house move.

    persist=False is the observe-only mode: decode, detect, match and answer,
    nothing else. No activity lookup, no attendance read, no background task
    - so no image/thumbnail, Upload, FaceDetection, Attendance or live-feed
    event. Matched entries say checkin_status "not_recorded" and the response
    carries "persisted": false, so an observed identity can never be mistaken
    for a recorded check-in. The default (persist=True) response is unchanged.

    observed, when a caller passes a dict, receives the decoded image size
    ({"w", "h"}) - the size the response's pixel boxes refer to. It is not
    part of the response.
    """
    t0 = time.perf_counter()
    t_read = time.perf_counter()

    img = storage_service.decode_image(file_bytes)
    if img is None:
        raise HTTPException(400, "Could not decode the uploaded image.")
    t_decode = time.perf_counter()
    if observed is not None:
        observed.update(w=int(img.shape[1]), h=int(img.shape[0]))

    working_img, scale = resize_for_detection(img)
    t_resize = time.perf_counter()

    inner_timings: dict = {}
    faces = detect_faces(working_img, timings=inner_timings)

    threshold = settings_cache.get_threshold()
    if faces:
        embeddings = np.stack([f.embedding for f in faces])
        matches = recognition_index.match_batch(embeddings, threshold)
    else:
        matches = []
    t_match = time.perf_counter()

    # Check-in status is decided here, against the real Attendance table, not
    # from any frontend/session state — so it survives page reloads, camera
    # restarts, and multiple kiosks pointed at the same backend. Each unique
    # matched person is looked up once even if they appear twice in one frame.
    # This is the one intentional DB read on the hot path — a single indexed
    # point lookup per unique matched person (usually 0-3 per frame).
    # Which activity this scan checks people into. Resolved once per request
    # so the synchronous decision and the background write cannot disagree if
    # an admin switches activity mid-scan.
    # Which activity this scan checks people into. A station pins its own
    # activity in its URL and sends it here, so several stations can run at
    # once against different activities; the app-wide current activity is the
    # fallback for a kiosk that pins nothing. Resolved once per request so the
    # synchronous decision and the background write cannot disagree.
    checkin_status: dict[str, str] = {}
    new_person_ids: set[str] = set()
    if persist:
        activity_id = _resolve_activity(session, activity_id)
        for person_id, *_ in matches:
            if person_id and person_id not in checkin_status:
                already = _checked_in_today(session, person_id, activity_id)
                checkin_status[person_id] = "already_checked_in" if already else "new"
                if not already:
                    new_person_ids.add(person_id)
    t_checkin = time.perf_counter()
    addresses = call_as_for(session, (m[0] for m in matches))

    results = []
    scaled_bboxes = []
    matched_count = 0
    unknown_count = 0
    for face, (person_id, first_name, full_name, participant_id, score) in zip(faces, matches):
        x1, y1, x2, y2 = face.bbox
        bbox = (x1 / scale, y1 / scale, x2 / scale, y2 / scale)
        scaled_bboxes.append(bbox)

        status = "matched" if person_id else "unknown"
        entry = {"bbox": list(bbox), "confidence": score, "status": status}  # [x1, y1, x2, y2], same convention as /api/uploads
        if person_id:
            matched_count += 1
            entry.update({
                "person_id": person_id,
                "participant_id": participant_id,
                "first_name": first_name,
                "name": full_name,
                "call_as": addresses.get(person_id),
                "checkin_status": checkin_status[person_id] if persist else "not_recorded",  # "new" | "already_checked_in" | "not_recorded"
            })
        else:
            unknown_count += 1
        results.append(entry)

    total_ms = (time.perf_counter() - t0) * 1000

    response = {
        "faces_total": len(faces),
        "faces_matched": matched_count,
        "faces_unknown": unknown_count,
        "processing_duration_ms": total_ms,
        "threshold_used": threshold,
        "results": results,
    }
    if not persist:
        response["persisted"] = False

    show_debug = debug if debug is not None else settings_cache.get_debug_mode()
    if show_debug:
        detect_embed_ms = inner_timings.get("detection_ms", 0.0) + inner_timings.get("embedding_ms", 0.0)
        comparison_ms = (t_match - t_resize) * 1000 - detect_embed_ms
        response["timings_ms"] = {
            "upload_read": round((t_read - t0) * 1000, 1),
            "image_decode": round((t_decode - t_read) * 1000, 1),
            "image_resize": round((t_resize - t_decode) * 1000, 1),
            "face_detection": round(inner_timings.get("detection_ms", 0.0), 1),
            "embedding": round(inner_timings.get("embedding_ms", 0.0), 1),
            "comparison": round(max(comparison_ms, 0.0), 1),
            "database_read": round((t_checkin - t_match) * 1000, 1),  # check-in status lookup, indexed point query per matched person
            "database_write": 0.0,  # backgrounded — see server log for actual cost
            "image_save": 0.0,  # backgrounded — see server log for actual cost
            "total": round(total_ms, 1),
        }
        response["system"] = {
            "inference_device": get_active_provider(),
            "model_name": MODEL_NAME,
            "registered_faces_indexed": recognition_index.size(),
        }

    if persist:
        background_tasks.add_task(_persist_recognition, file_bytes, filename, user_id, threshold, faces, matches, scaled_bboxes, new_person_ids, activity_id, feed_node_id, source_kind)

    return response


@router.post("/upload")
def recognize(
    background_tasks: BackgroundTasks,
    photo: UploadFile = File(...),
    debug: bool | None = None,
    activity_id: str | None = Form(None),
    persist: bool = True,
    session: Session = Depends(get_session),
    user: User = Depends(get_api_client),
):
    """Recognize-first, persist-second: everything up to the return in
    run_recognition() is in-memory only (no DB query, no DB write, no disk
    write) so the identity result comes back as fast as detection + embedding
    + comparison allow. Storage and history bookkeeping happen in a
    background task afterward. Thin wrapper around run_recognition() — see
    its docstring for why this is shared rather than duplicated.

    ?persist=false (query) recognizes without saving or checking in anyone.
    A machine account (robot) may only use persist=false: it observes, it is
    never a check-in station.
    """
    if is_machine(user) and persist:
        raise HTTPException(403, "Machine accounts may only call this with persist=false.")
    file_bytes = photo.file.read()
    filename = photo.filename or "upload.jpg"
    return run_recognition(
        file_bytes, filename,
        background_tasks=background_tasks, session=session,
        user_id=user.id, activity_id=activity_id, debug=debug, persist=persist,
    )
