"""Distributed camera nodes: registration, presence, recognition-index sync,
recognition events, browser-side recording, and WebRTC/CCTV signaling.

ARCHITECTURE (see the task's own diagram — this file is Central's half of it)

    Windows PC station:
        browser (owns the camera) -> Local Node Agent on THAT PC -> HERE
        (the agent does its own InsightFace inference; this file only
        receives the RESULT and makes the shared decision: attendance)

    Phone / iPad:
        browser (owns the camera) -> HERE -> InsightFace (Central's own GPU)
        (no local agent exists on a phone; recognition itself runs here,
        by calling straight into api.recognition.run_recognition())

Central remains the single source of truth either way: which participant is
valid, which activity is current, whether they already checked in today, and
the permanent recognition history are ALL decided here, never by a node.

WHAT IS DELIBERATELY NOT HERE
    No node has, or is given, a copy of the authoritative database. A Local
    Agent gets only recognition_index.snapshot() (participant_id, a display
    name, and a raw embedding) - never emails, image paths, or anything else
    Person carries. See face_recognition/index.py's snapshot() docstring.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.recognition import _checked_in_today, _resolve_activity, run_recognition
from app.auth.deps import get_current_user
from app.auth.security import decode_access_token
from app.config import NODE_RECORDINGS_DIR, STORAGE_PATH
from app.database.db import get_session
from app.face_recognition.index import recognition_index
from app.models.models import (
    Activity,
    Attendance,
    CameraNode,
    FaceDetection,
    NodeRecording,
    Person,
    Upload,
    User,
)
from app.services import events_feed, node_registry, storage_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/node", tags=["nodes"])


def _activity_name(session: Session, activity_id: str | None) -> str:
    if not activity_id:
        return ""
    activity = session.get(Activity, activity_id)
    return activity.name if activity else ""


# ---------------------------------------------------------------------------
# Registration and presence
# ---------------------------------------------------------------------------

class NodeRegisterBody(BaseModel):
    node_id: str
    display_name: str = ""
    activity_id: str | None = None
    camera_label: str = ""
    mode: str = "always"           # tap | always
    inference_mode: str = "local"  # local | central


@router.post("/register")
def register_node(body: NodeRegisterBody, session: Session = Depends(get_session),
                  user: User = Depends(get_current_user)):
    """Create or update this node's durable identity. Safe to call again on
    every page load — that is how identity survives a refresh (§18)."""
    node_id = body.node_id.strip()
    if not node_id:
        raise HTTPException(400, "node_id is required.")

    row = session.exec(select(CameraNode).where(CameraNode.node_id == node_id)).first()
    if not row:
        row = CameraNode(node_id=node_id)
    row.display_name = body.display_name or row.display_name or node_id
    row.activity_id = body.activity_id
    row.camera_label = body.camera_label
    row.mode = body.mode
    row.inference_mode = body.inference_mode
    row.updated_at = datetime.now()
    session.add(row)
    session.commit()
    session.refresh(row)

    node_registry.touch(
        node_id, display_name=row.display_name, activity_id=row.activity_id,
        activity_name=_activity_name(session, row.activity_id),
        camera_label=row.camera_label, mode=row.mode, inference_mode=row.inference_mode,
    )
    return {
        "node_id": row.node_id, "display_name": row.display_name,
        "activity_id": row.activity_id, "camera_label": row.camera_label,
        "mode": row.mode, "inference_mode": row.inference_mode,
        "index_version": recognition_index.version(),
    }


@router.get("/config/{node_id}")
def get_node_config(node_id: str, session: Session = Depends(get_session),
                    user: User = Depends(get_current_user)):
    """What a station remembers about itself across a page refresh."""
    row = session.exec(select(CameraNode).where(CameraNode.node_id == node_id)).first()
    if not row:
        raise HTTPException(404, "This node has not registered yet.")
    return {
        "node_id": row.node_id, "display_name": row.display_name,
        "activity_id": row.activity_id, "camera_label": row.camera_label,
        "mode": row.mode, "inference_mode": row.inference_mode,
    }


class HeartbeatBody(BaseModel):
    node_id: str
    recording: bool | None = None
    local_agent_ok: bool | None = None
    faces_seen: int | None = None
    index_version: int | None = None


@router.post("/heartbeat")
def heartbeat(body: HeartbeatBody, user: User = Depends(get_current_user)):
    """Cheap, frequent, and deliberately writes NOTHING to the database (§20)
    — see node_registry.py for why presence lives in memory only."""
    node = node_registry.touch(
        body.node_id, recording=body.recording, local_agent_ok=body.local_agent_ok,
        faces_seen=body.faces_seen, index_version=body.index_version,
    )
    return {
        "server_time": datetime.now().isoformat(),
        "current_index_version": recognition_index.version(),
        "node": node,
    }


@router.get("/status")
def node_status(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    """Every node this backend has heard from, online or not (§20). Merges
    live presence (memory) with durable config (database) for display."""
    configs = {c.node_id: c for c in session.exec(select(CameraNode)).all()}
    out = []
    for live in node_registry.status_all():
        cfg = configs.get(live["node_id"])
        out.append({**live, "camera_label": cfg.camera_label if cfg else live.get("camera_label", "")})
    return {"nodes": out, "current_index_version": recognition_index.version()}


# ---------------------------------------------------------------------------
# Recognition-index sync — what a Local Agent needs to build its own index
# ---------------------------------------------------------------------------

@router.get("/recognition-index")
def get_recognition_index(user: User = Depends(get_current_user)):
    """A Local Agent polls this and compares `version` to its own. On a
    mismatch it re-downloads the whole thing and rebuilds — a safe full
    refresh, not incremental diffing (§22: "a safe full-index refresh is
    acceptable for Dummy"). Threshold travels with it so a node's local
    match_batch() call uses the SAME cutoff Central does."""
    from app.services import settings_cache

    return {
        "version": recognition_index.version(),
        "threshold": settings_cache.get_threshold(),
        "participants": recognition_index.snapshot(),
    }


# ---------------------------------------------------------------------------
# Recognition events — Local Agent already decided WHO; Central decides
# whether that becomes an attendance record.
# ---------------------------------------------------------------------------

class AgentMatch(BaseModel):
    participant_id: str
    confidence: float


class NodeEventBody(BaseModel):
    node_id: str
    camera_label: str = ""   # display-only — e.g. "Logitech C920", threaded into the Upload filename
    activity_id: str | None = None
    matches: list[AgentMatch] = []


# Multiple physical cameras on one PC can each report a match for the SAME
# participant within milliseconds of each other (§15/§27) - two truly
# concurrent requests could both read "not checked in today" before either
# had committed its Attendance row, which would create a duplicate despite
# _checked_in_today() being correct in isolation. This lock closes exactly
# that window: the check and the write for a given event happen as one
# atomic step process-wide, not per-camera. Attendance decisions are rare
# and cheap, so serializing them here costs nothing worth measuring.
_attendance_lock = threading.Lock()


@router.post("/event")
def node_event(
    body: str = Form(...),          # JSON-encoded NodeEventBody — multipart needs this alongside the file
    image: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """A Windows Local Agent reports a recognition RESULT it already computed
    locally. Central's job here is exactly §34: validate the participant,
    resolve the activity, decide already-checked-in-or-not, and create the
    Attendance row — the same shared decision the kiosk itself makes, just
    fed a pre-computed match instead of a raw frame.

    The Upload/FaceDetection/Attendance rows are created together, in this
    one request, rather than split into a background task: unlike the kiosk
    (which optimizes for a person standing at a screen waiting on the
    result), the slow part here — detection and embedding — already happened
    on the node. What is left is a handful of small inserts, and Attendance
    needs a real face_detection_id to point at, so there is nothing later to
    "backfill" it with.
    """
    payload = NodeEventBody.model_validate_json(body)
    activity_id = _resolve_activity(session, payload.activity_id)
    image_bytes = image.file.read() if image is not None else None

    image_path = ""
    if image_bytes:
        image_path, _thumb = storage_service.save_event_image(image_bytes, f"{payload.node_id}_event.jpg")

    source_label = f"{payload.node_id} ({payload.camera_label})" if payload.camera_label else payload.node_id

    matched_people: list[Person] = []
    for m in payload.matches:
        person = session.exec(select(Person).where(Person.participant_id == m.participant_id)).first()
        if person:
            matched_people.append(person)
        # else: the agent's cached index is stale relative to Central —
        # ignore that match rather than guess who it might have meant.

    upload = Upload(
        filename=f"{source_label} event", image_path=image_path,
        processing_status="completed", faces_total=max(len(payload.matches), len(matched_people)),
        faces_matched=len(matched_people), faces_unknown=0,
    )
    session.add(upload)
    session.commit()
    session.refresh(upload)

    seen: list[dict] = []
    for person, m in zip(matched_people, payload.matches):
        detection = FaceDetection(
            upload_id=upload.id, person_id=person.id,
            confidence=m.confidence, bbox="0,0,0,0", status="matched",
        )
        session.add(detection)
        session.commit()
        session.refresh(detection)

        with _attendance_lock:
            already = _checked_in_today(session, person.id, activity_id)
            if not already:
                session.add(Attendance(
                    person_id=person.id, upload_id=upload.id, face_detection_id=detection.id,
                    confidence=m.confidence, activity_id=activity_id or None,
                ))
                session.commit()

        seen.append({
            "person_id": person.id, "participant_id": person.participant_id,
            "name": f"{person.first_name} {person.last_name}".strip() or person.first_name,
            "confidence": m.confidence, "checkin": "already" if already else "new",
        })

    node_registry.touch(payload.node_id, faces_seen=len(payload.matches))
    for s in seen:
        events_feed.publish({
            "node_id": payload.node_id, "participant_id": s["participant_id"],
            "name": s["name"], "activity_id": activity_id, "checkin": s["checkin"],
            "at": datetime.now().isoformat(),
        })

    return {"results": seen, "activity_id": activity_id}


@router.post("/recognize-central")
def recognize_central(
    background_tasks: BackgroundTasks,
    node_id: str = Form(...),
    photo: UploadFile = File(...),
    activity_id: str | None = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Mobile/iPad recognition (§25-29): the SAME recognition-and-checkin
    core the kiosk uses (run_recognition), reused rather than reimplemented —
    the only difference is the filename tag, which is how the existing
    Upload history already distinguishes a source (camera_recognition.py
    does the same thing with f"{camera_name} live" for the ffmpeg cameras).
    Central performs the inference; the phone never runs InsightFace itself.
    """
    file_bytes = photo.file.read()
    result = run_recognition(
        file_bytes, f"{node_id}_scan.jpg",
        background_tasks=background_tasks, session=session,
        user_id=user.id, activity_id=activity_id, debug=False,
    )
    node_registry.touch(node_id, faces_seen=result.get("faces_total", 0), inference_mode="central")
    resolved_activity = _resolve_activity(session, activity_id)
    for r in result.get("results", []):
        if r.get("status") == "matched":
            events_feed.publish({
                "node_id": node_id, "participant_id": r.get("participant_id"),
                "name": r.get("name") or r.get("first_name"), "activity_id": resolved_activity,
                "checkin": "new" if r.get("checkin_status") == "new" else "already",
                "at": datetime.now().isoformat(),
            })
    return result


# ---------------------------------------------------------------------------
# Browser-side recording (MediaRecorder output), attributable per node (§17)
# ---------------------------------------------------------------------------

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(s: str) -> str:
    return _UNSAFE_FILENAME_RE.sub("_", s.strip()) or "x"


class RecordingStartBody(BaseModel):
    node_id: str
    node_name: str = ""
    activity_id: str | None = None


@router.post("/recording/start")
def start_node_recording(body: RecordingStartBody, session: Session = Depends(get_session),
                         user: User = Depends(get_current_user)):
    row = NodeRecording(
        node_id=body.node_id, node_name=body.node_name or body.node_id,
        activity_id=body.activity_id, activity_name=_activity_name(session, body.activity_id),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    node_registry.touch(body.node_id, recording=True)
    return {"recording_id": row.id}


@router.post("/recording/stop")
def stop_node_recording(
    recording_id: str = Form(...),
    clip: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    row = session.get(NodeRecording, recording_id)
    if not row:
        raise HTTPException(404, "Recording not found")
    node_dir = NODE_RECORDINGS_DIR / row.node_id
    node_dir.mkdir(parents=True, exist_ok=True)
    ext = ".webm"  # MediaRecorder's default container on every browser this targets
    if clip.filename and "." in clip.filename:
        ext = "." + clip.filename.rsplit(".", 1)[-1]
    # Human-readable name (Task 1D §12) — Activity/camera/timestamp, so a
    # folder of clips is browsable without opening each one. Uniqueness does
    # not actually depend on this: node_dir is already per-camera and row.id
    # is a fresh id per recording, so two cameras or two takes can never
    # collide even if the readable part happened to repeat — the id suffix
    # is belt-and-braces, not the thing preventing collisions.
    activity_part = _safe_filename_part(row.activity_name) if row.activity_name else "NoActivity"
    camera_part = _safe_filename_part(row.node_id)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = node_dir / f"{activity_part}_{camera_part}_{timestamp}_{row.id[:8]}{ext}"
    data = clip.file.read()
    file_path.write_bytes(data)

    row.file_path = str(file_path.relative_to(STORAGE_PATH))  # same convention as Person.image_path
    row.ended_at = datetime.now()
    row.status = "completed"
    row.size_bytes = len(data)
    session.add(row)
    session.commit()

    node_registry.touch(row.node_id, recording=False)
    return {"recording_id": row.id, "size_bytes": row.size_bytes, "file_path": row.file_path}


@router.get("/recordings")
def list_node_recordings(node_id: str | None = None, limit: int = 100,
                         session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    stmt = select(NodeRecording).order_by(NodeRecording.started_at.desc()).limit(limit)
    if node_id:
        stmt = stmt.where(NodeRecording.node_id == node_id)
    return {"recordings": session.exec(stmt).all()}


# ---------------------------------------------------------------------------
# WebRTC signaling relay — Central never touches media, only SDP/ICE (§41)
# ---------------------------------------------------------------------------

class _SignalHub:
    """One room per node_id. A publisher (the camera) and any number of
    viewers (CCTV tiles) join the same room; a message from one member is
    relayed to every OTHER member. No media, no recording, no persistence —
    purely a mailbox, so this stays a plain in-memory dict."""

    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    async def join(self, room: str, ws: WebSocket) -> None:
        self._rooms.setdefault(room, set()).add(ws)

    def leave(self, room: str, ws: WebSocket) -> None:
        members = self._rooms.get(room)
        if members:
            members.discard(ws)
            if not members:
                self._rooms.pop(room, None)

    async def relay(self, room: str, sender: WebSocket, message: str) -> None:
        for member in list(self._rooms.get(room, ())):
            if member is not sender:
                try:
                    await member.send_text(message)
                except Exception:  # noqa: BLE001 — a dead peer must not break the others
                    pass


_signal_hub = _SignalHub()


def _ws_user(token: str | None) -> bool:
    if not token:
        return False
    return decode_access_token(token) is not None


@router.websocket("/signal")
async def signal(websocket: WebSocket, room: str, token: str | None = None):
    """WebRTC signaling for one node's room. Every message this relays is
    opaque SDP/ICE JSON from the browser's own RTCPeerConnection — this
    endpoint never parses or acts on it, only forwards it to the room's other
    member(s)."""
    if not _ws_user(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await _signal_hub.join(room, websocket)
    try:
        while True:
            message = await websocket.receive_text()
            await _signal_hub.relay(room, websocket, message)
    except WebSocketDisconnect:
        pass
    finally:
        _signal_hub.leave(room, websocket)


@router.websocket("/events-feed")
async def events_feed_ws(websocket: WebSocket, token: str | None = None):
    """Push channel for the CCTV page's global Recent Detections (§44) — a
    subscriber gets a JSON line every time /event or /recognize-central
    records a match, instead of the page polling recognition history."""
    if not _ws_user(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = events_feed.subscribe()
    try:
        while True:
            item = await queue.get()
            await websocket.send_text(json.dumps(item))
    except WebSocketDisconnect:
        pass
    finally:
        events_feed.unsubscribe(queue)
