"""Local video experiment: no Drive, Event Batch, attendance or DB writes.

Configured-camera v3: completion-paced scans with target-rate and virtual FPS limits.
A virtual source clock advances without sleeping; decode/discard intervening
frames sequentially. Timestamps are nominal index/FPS, not container PTS.
Each distinct saved frame is inferred at most once; no inference queue.
"""
from __future__ import annotations
import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import cv2
import numpy as np
from sqlmodel import Session, select
from app.config import BACKEND_DIR
from app.database.db import engine as db_engine
from app.face_recognition.index import RecognitionIndex
from app.models.models import Person
from app.services import settings_cache
from app.services.scan_settings import ScanSettings, next_scan_start

ROOT = BACKEND_DIR / "local_video_experiment_storage"
MAX_BYTES = 512 * 1024 * 1024
MAX_DURATION = 1800
MAX_FPS = 120
MAX_PIXELS = 3840 * 2160
PREVIEW_MAX_SIDE = 1920
VERDICTS = {"Not reviewed", "Correct", "Incorrect", "Unsure"}
SAMPLE_HZ = None  # Historical completion-paced loop has no fixed detection Hz.
POST_SCAN_DELAY_SECONDS = 0.4
MAX_SCAN_HZ = 1 / POST_SCAN_DELAY_SECONDS
HISTORICAL_COMMIT = "e4dbf60a68c3fb8b6cfd7705d1ef4f70cff2d559"
METHOD = "configured-camera-v3: latest distinct virtual-camera frame; max(scan finish + wait, scan start + 1/target); measured work clock"
TIMING_DIFFERENCES = ("Single-camera offline approximation: no browser JPEG/HTTP/Central event latency, "
    "no multi-camera backlog/503 or original dropped-frame log. Measured inference/matching lock wait "
    "is included. Decode/checkpoint overhead affects wall throughput, not virtual cadence. "
    "Virtual-camera availability is limited by configured camera FPS. Low FPS waits for a distinct available frame; missing/VFR timeline cannot be reconstructed from nominal FPS.")
ACTIVE = {"running", "cancelling"}
EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
_lock = threading.RLock()
_initialized = False
_active: str | None = None
_cancel = threading.Event()
logger = logging.getLogger(__name__)

class NotFound(LookupError):
    pass
class Busy(ValueError):
    pass
class MediaError(ValueError):
    pass

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def filename(value: str) -> str:
    # Client name is display metadata only. Reject paths, do not basename them.
    if (not value or len(value) > 180 or value in {".", ".."}
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(c in value for c in '/\\:*?"<>|') or value.endswith((" ", "."))):
        raise MediaError("Use a simple video filename without paths or control characters.")
    if Path(value).suffix.lower() not in EXTENSIONS:
        raise MediaError("Supported containers: MP4, MOV, AVI, MKV, WebM, M4V. Codec must be decodable by OpenCV/FFmpeg.")
    return value

def directory(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        raise NotFound("Experiment not found")
    target = ROOT / job_id
    if ROOT.is_symlink() or target.is_symlink() or target.resolve().parent != ROOT.resolve():
        raise NotFound("Experiment not found")
    return target

def _read(job_id: str) -> dict:
    path = directory(job_id) / "state.json"
    if path.is_symlink():
        raise NotFound("Experiment not found")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["id"] != job_id:
            raise NotFound("Experiment not found")
        return state
    except (OSError, ValueError, KeyError) as exc:
        raise NotFound("Experiment not found") from exc

def _write(state: dict) -> dict:
    path = directory(state["id"])
    temp = path / ".state.writing"
    temp.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    deadline = time.monotonic() + 3
    while True:
        try:
            os.replace(temp, path / "state.json")
            return copy.deepcopy(state)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)

def initialize() -> None:
    """Once per process; persisted active jobs never spawn workers."""
    global _initialized
    with _lock:
        if _initialized:
            return
        if ROOT.exists() and not ROOT.is_symlink():
            for path in ROOT.iterdir():
                try:
                    state = _read(path.name)
                except NotFound:
                    continue
                if state["status"] in ACTIVE | {"uploading"}:
                    state.update(status="interrupted", finished_at=now(),
                        error="Backend restarted. Partial results preserved; upload a new experiment to run again.")
                    _write(state)
        _initialized = True

def get(job_id: str) -> dict:
    initialize()
    with _lock:
        return _read(job_id)

def list_jobs() -> list[dict]:
    initialize()
    with _lock:
        result = []
        if ROOT.exists() and not ROOT.is_symlink():
            for path in ROOT.iterdir():
                try:
                    state = _read(path.name)
                    if state["status"] != "uploading":
                        result.append(state)
                except NotFound:
                    continue
        return sorted(result, key=lambda s: s["created_at"], reverse=True)

def allocate(name: str) -> dict:
    initialize()
    name = filename(name)
    with _lock:
        if ROOT.is_symlink():
            raise MediaError("Experiment storage is unavailable.")
        ROOT.mkdir(parents=True, exist_ok=True)
        job_id = secrets.token_hex(16)
        directory(job_id).mkdir()
        return _write(dict(id=job_id, filename=name, status="uploading", created_at=now(),
            started_at=None, finished_at=None, error=None, media=None,
            sampling_hz=SAMPLE_HZ, sampling_method=METHOD, model=None,
            post_scan_delay_seconds=POST_SCAN_DELAY_SECONDS, historical_reference=HISTORICAL_COMMIT,
            timing_differences=TIMING_DIFFERENCES, scan_work_seconds=0.0, scan_settings=None,
            sampled_frames=0, decoded_frames=0, unknown_detections=0,
            frames_with_unknown=0, matched_face_detections=0,
            processing_seconds=0.0, people=[], samples=[]))

def video_path(state: dict) -> Path:
    path = directory(state["id"]) / ("video" + Path(state["filename"]).suffix.lower())
    if path.is_symlink() or path.resolve().parent != directory(state["id"]).resolve():
        raise NotFound("Experiment media not found")
    return path

def identity_key(job_id: str, person_id: str) -> str:
    return hashlib.sha256((job_id + person_id).encode()).hexdigest()[:16]


def preview_path(job_id: str, key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", key or ""):
        raise NotFound("Match preview not found")
    folder = directory(job_id) / "previews"
    path = folder / (key + ".jpg")
    if folder.is_symlink() or path.is_symlink() or path.resolve().parent != folder.resolve():
        raise NotFound("Match preview not found")
    return path


def save_representative(state: dict, row: dict, face, match, image, frame_index: int, timestamp: float) -> None:
    """One whole frame per identity. Highest cosine similarity; earliest tie.

    Detector bbox is already in full source-image coordinates. Scale x/y by
    actual output dimensions (integer resize rounding), not a guessed ratio.
    Missing/invalid bbox never drops the automatic count; it cannot be reviewed.
    """
    try:
        bbox = np.asarray(face.bbox, dtype=float).reshape(4)
    except (AttributeError, TypeError, ValueError):
        return
    height, width = image.shape[:2]
    if not np.isfinite(bbox).all():
        return
    x1, y1, x2, y2 = bbox.tolist()
    x1, x2 = max(0.0, min(width, x1)), max(0.0, min(width, x2))
    y1, y2 = max(0.0, min(height, y1)), max(0.0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        return
    raw_score = getattr(match, "score", None)
    score = float(raw_score) if isinstance(raw_score, (int, float, np.number)) and math.isfinite(raw_score) and -1.000001 <= raw_score <= 1.000001 else None
    if score is not None:
        score = max(-1.0, min(1.0, score))
    previous = row.get("preview")
    if previous:
        old_score = previous.get("match_score")
        # A comparable match supersedes an unscored example. Unscored matches
        # never displace a scored example; equal scores retain earliest order.
        if score is None or (old_score is not None and score <= old_score):
            return
    scale = min(1.0, PREVIEW_MAX_SIDE / max(width, height))
    out_width, out_height = max(1, round(width * scale)), max(1, round(height * scale))
    full_frame = image if (out_width, out_height) == (width, height) else cv2.resize(image, (out_width, out_height), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", full_frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise MediaError("Could not save representative match frame. Partial results retained.")
    path = preview_path(state["id"], identity_key(state["id"], row["person_id"]))
    path.parent.mkdir(exist_ok=True)
    temp = path.with_suffix(".writing")
    try:
        temp.write_bytes(encoded.tobytes())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    row["preview"] = dict(frame_index=frame_index, frame_number=frame_index + 1,
        timestamp_seconds=timestamp, source_timestamp_seconds=state.get("decoded_timestamp_last_seconds"), match_score=score,
        score_meaning="cosine similarity (higher is stronger)" if score is not None else "unavailable",
        selection_rule="strongest valid cosine similarity; earliest tie" if score is not None else "earliest valid match without comparable score",
        source_width=width, source_height=height, source_bbox=[x1, y1, x2, y2],
        width=out_width, height=out_height,
        bbox=[x1 * out_width / width, y1 * out_height / height, x2 * out_width / width, y2 * out_height / height],
        image_sha256=hashlib.sha256(encoded.tobytes()).hexdigest(),
        example_key=hashlib.sha256(encoded.tobytes() + json.dumps([frame_index, x1, y1, x2, y2, score]).encode()).hexdigest()[:32])


def _reviews(job_id: str) -> dict:
    path = directory(job_id) / "reviews.json"
    if path.is_symlink():
        raise NotFound("Reviews unavailable")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotFound("Reviews unavailable") from exc


def review_example(job_id: str, key: str, verdict: str, example_key: str) -> dict:
    if verdict not in VERDICTS:
        raise MediaError("Choose Not reviewed, Correct, Incorrect or Unsure.")
    with _lock:
        state = get(job_id)
        if state["status"] in ACTIVE or _active == job_id:
            raise Busy("Wait for processing to stop before reviewing its final example.")
        row = next((p for p in state["people"] if identity_key(job_id, p["person_id"]) == key), None)
        if not row or not row.get("preview") or not preview_path(job_id, key).is_file():
            raise NotFound("Preview unavailable for this older run")
        if row["preview"]["example_key"] != example_key:
            raise Busy("Shown example changed. Open the current match preview before reviewing.")
        reviews = _reviews(job_id)
        reviews[key] = dict(verdict=verdict, example_key=example_key, reviewed_at=now(),
            meaning="verdict applies only to the shown representative example")
        path = directory(job_id) / "reviews.json"
        temp = path.with_suffix(".writing")
        try:
            temp.write_text(json.dumps(reviews, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return public(state)


def open_capture(path: Path):
    # Fixed backend, server-controlled path; no image-sequence/network input.
    return cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)

def next_capture(frame_index: int, capture_time: float, work_seconds: float, fps: float,
                 settings: ScanSettings | None = None) -> tuple[int, float]:
    """Latest source frame at the latest virtual-camera tick before the deadline.

    If already inferred, advance to the first virtual tick exposing a distinct
    source frame. Never repeat frames or queue scans to catch up.
    """
    settings = settings or ScanSettings()
    deadline = next_scan_start(capture_time, capture_time + max(0.0, work_seconds), settings)
    camera_fps = settings.camera_fps
    tick = math.floor(deadline * camera_fps + 1e-9)
    index = math.floor(tick * fps / camera_fps + 1e-9)
    if index <= frame_index:
        tick = max(tick + 1, math.ceil((frame_index + 1) * camera_fps / fps - 1e-9))
        index = math.floor(tick * fps / camera_fps + 1e-9)
    return index, max(deadline, tick / camera_fps)


def maximum_sample_count(frames: int, fps: float, settings: ScanSettings | None = None) -> int:
    """Upper bound with zero scan work. Actual count depends on this run."""
    index, capture_time, count = 0, 0.0, 0
    while index < frames:
        count += 1
        index, capture_time = next_capture(index, capture_time, 0.0, fps, settings)
    return count


def probe(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(32)
    is_avi = header[:4] == b"RIFF" and header[8:12] == b"AVI "
    is_ebml = header[:4] == b"\x1a\x45\xdf\xa3"
    is_mov = header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}
    if not (is_avi or is_ebml or is_mov):
        raise MediaError("Invalid video container. Playlists, image sequences and network sources are not supported.")
    cap = open_capture(path)
    try:
        if not cap.isOpened():
            raise MediaError("Invalid video or unsupported codec. Try MP4/H.264 or AVI/MJPEG.")
        fps, count = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        width, height = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if not all(math.isfinite(v) and v > 0 for v in (fps, count, width, height)):
            raise MediaError("No reliable FPS, frame count or dimensions. Export a constant-frame-rate MP4 and retry.")
        frames = round(count)
        duration = frames / fps
        if fps < 1.0 or fps > MAX_FPS or duration > MAX_DURATION or width * height > MAX_PIXELS:
            raise MediaError("Video limit: 30 minutes, 1-120 FPS, at most 3840x2160 pixels. Export a smaller constant-frame-rate video.")
        ok, first = cap.read()
        if not ok or first is None or first.size == 0:
            raise MediaError("First frame cannot be decoded. Try MP4/H.264 or AVI/MJPEG.")
        if first.shape[0] * first.shape[1] > MAX_PIXELS:
            raise MediaError("Decoded video exceeds the 4K pixel limit.")
        return dict(fps=fps, frame_count=frames, duration_seconds=duration,
            width=int(width), height=int(height), decoder="OpenCV/FFmpeg", opencv_version=cv2.__version__,
            expected_samples=None, max_samples=maximum_sample_count(frames, fps))
    except cv2.error as exc:
        raise MediaError("Unable to decode video. Try MP4/H.264 or AVI/MJPEG.") from exc
    finally:
        cap.release()

def complete_upload(job_id: str, size: int, sha256: str, media: dict) -> dict:
    with _lock:
        state = _read(job_id)
        state.update(status="ready", media={**media, "bytes": size, "sha256": sha256})
        return _write(state)

def remove(job_id: str) -> None:
    initialize()
    with _lock:
        state = _read(job_id)
        if state["status"] in ACTIVE or _active == job_id:
            raise Busy("Cancel and wait for processing to stop before Delete.")
        # Resolved target verified by directory(); never any other storage.
        shutil.rmtree(directory(job_id))

def discard_upload(job_id: str) -> None:
    with _lock:
        state = _read(job_id)
        if state["status"] == "uploading":
            shutil.rmtree(directory(job_id))

def identity_snapshot() -> tuple[RecognitionIndex, dict]:
    """Read identities once, freeze matcher and current configuration."""
    from app.face_recognition import engine
    app = engine.get_face_app()
    with Session(db_engine) as session:
        rows = session.exec(select(Person.id, Person.first_name, Person.last_name, Person.participant_id, Person.embedding)).all()
        people = [SimpleNamespace(id=r[0], first_name=r[1], last_name=r[2], participant_id=r[3], embedding=r[4]) for r in rows]
    index = RecognitionIndex()
    index.rebuild(people)
    return index, dict(name=engine.MODEL_NAME, provider=engine.get_active_provider(),
        detector_provider=app.det_model.session.get_providers()[0],
        threshold=settings_cache.get_threshold(), detection_threshold=float(app.det_model.det_thresh),
        detection_size=list(app.det_model.input_size), enrolled_identities=index.size())

def detect(image):
    from app.face_recognition.engine import detect_faces
    return detect_faces(image)

def start(job_id: str, settings: ScanSettings | dict | None = None) -> dict:
    global _active
    settings = settings if isinstance(settings, ScanSettings) else ScanSettings.model_validate({} if settings is None else settings)
    initialize()
    with _lock:
        state = _read(job_id)
        if _active is not None or state["status"] != "ready":
            raise Busy("Only one local video experiment can run; only a ready upload can start.")
        _active = job_id
        _cancel.clear()
        state.update(status="running", started_at=now(), sampling_hz=SAMPLE_HZ, sampling_method=METHOD,
            post_scan_delay_seconds=settings.post_scan_delay_seconds, historical_reference=HISTORICAL_COMMIT,
            timing_differences=TIMING_DIFFERENCES, scan_work_seconds=0.0,
            scan_settings=settings.model_dump(), preview_version=1)
        state["media"].update(expected_samples=None,
            max_samples=maximum_sample_count(state["media"]["frame_count"], state["media"]["fps"], settings),
            virtual_capture_fps_limit=settings.camera_fps,
            effective_available_fps_limit=min(settings.camera_fps, state["media"]["fps"]))
        _write(state)
        try:
            threading.Thread(target=run, args=(job_id,), name="local-video-experiment", daemon=True).start()
        except Exception:
            _active = None
            state.update(status="failed", error="Could not start worker.", finished_at=now())
            _write(state)
            raise
        return state

def cancel(job_id: str) -> dict:
    with _lock:
        state = get(job_id)
        if state["status"] in ACTIVE:
            _cancel.set()
            state["status"] = "cancelling"
            return _write(state)
        return state

def _checkpoint(state: dict, clock: float) -> None:
    with _lock:
        state["processing_seconds"] = round(time.perf_counter() - clock, 6)
        if _cancel.is_set():
            state["status"] = "cancelling"
        _write(state)

def scan_clock() -> float:
    """Separate monotonic clock boundary for deterministic synthetic replay tests."""
    return time.perf_counter()


def verify_eof(path: Path, decoded_frames: int) -> dict:
    """Independent sequential decode to null; OpenCV cannot distinguish EOF/errors.

    No images are exported and no recognition is repeated. Passthrough timing
    prevents FFmpeg from duplicating VFR frames to meet a nominal output FPS.
    Metadata-only logs spool to temporary files, avoiding pipe deadlocks/RAM growth.
    """
    executable = shutil.which("ffmpeg")
    if not executable:
        return dict(outcome="unverified", verified_frame_count=None,
            error="EOF could not be verified: FFmpeg executable is unavailable. Partial results retained; install FFmpeg on PATH and upload a new experiment.")
    command = [executable, "-nostdin", "-v", "warning", "-xerror", "-err_detect", "explode",
        "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", "0:v:0", "-an",
        "-fps_mode", "passthrough", "-progress", "pipe:1", "-nostats", "-f", "null", "-"]
    with tempfile.TemporaryFile() as progress, tempfile.TemporaryFile() as diagnostics:
        try:
            process = subprocess.Popen(command, stdout=progress, stderr=diagnostics,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            return dict(outcome="unverified", verified_frame_count=None,
                error="EOF verifier could not start. Partial results retained; check FFmpeg availability and upload a new experiment.")
        deadline = time.monotonic() + 600
        aborted = None
        try:
            while process.poll() is None:
                if _cancel.is_set():
                    aborted = "cancelled"
                    break
                if time.monotonic() > deadline or progress.tell() > 2 * 1024 * 1024 or diagnostics.tell() > 2 * 1024 * 1024:
                    aborted = "verification_limit"
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        if aborted:
            return dict(outcome=aborted, verified_frame_count=None,
                error=None if aborted == "cancelled" else "EOF verification exceeded its time/log limit. Partial results retained.")
        progress.seek(0); diagnostics.seek(0)
        values = dict(line.split("=", 1) for line in progress.read(2 * 1024 * 1024).decode("utf-8", "replace").splitlines() if "=" in line)
        log = diagnostics.read(2 * 1024 * 1024).decode("utf-8", "replace").lower()
        count = int(values["frame"]) if values.get("frame", "").strip().isdigit() else None
        damaged = bool(re.search(r"file ended prematurely|invalid data|error|corrupt|truncat|partial file|overread|packet too small|element.*exceeds", log))
        if process.returncode != 0 or damaged:
            return dict(outcome="decode_error", verified_frame_count=count,
                error="Independent decoder reported damaged/truncated media or a decode error. Partial results retained; try a playable original or repaired export in a new experiment.")
        if values.get("progress") != "end" or count != decoded_frames:
            return dict(outcome="decoder_disagreement", verified_frame_count=count,
                error="OpenCV stopped before the independent decoder's readable end, or EOF was not confirmed. Partial results retained; try a supported export in a new experiment.")
        return dict(outcome="normal_eof", verified_frame_count=count, error=None,
            metadata_frame_count_is_estimate=True, verifier="FFmpeg sequential decode to null (no duplicate frames)")


def run(job_id: str) -> None:
    global _active
    clock = time.perf_counter()
    state = get(job_id)
    cap = None
    counts = {}
    try:
        settings = ScanSettings.model_validate(state["scan_settings"])
        index, state["model"] = identity_snapshot()
        _checkpoint(state, clock)
        cap = open_capture(video_path(state))
        if not cap.isOpened():
            raise MediaError("Video can no longer be opened. Try MP4/H.264 or AVI/MJPEG.")
        next_frame, capture_time = 0, 0.0
        while not _cancel.is_set():
            ok, image = cap.read()
            if not ok or image is None:
                state["decode_phase"] = "verifying_eof"
                _checkpoint(state, clock)
                state["decode_audit"] = verify_eof(video_path(state), state["decoded_frames"])
                if state["decode_audit"]["error"] and not _cancel.is_set():
                    raise MediaError(state["decode_audit"]["error"])
                break
            frame_index = state["decoded_frames"]
            if frame_index >= MAX_DURATION * state["media"]["fps"]:
                raise MediaError("Decoded nominal duration exceeds the 30 minute limit. Partial results retained.")
            # PTS is observational metadata only; v3 selection remains index/FPS.
            pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000 if hasattr(cap, "get") else None
            if pts is not None and math.isfinite(pts) and pts >= 0:
                previous_pts = state.get("decoded_timestamp_last_seconds")
                state.setdefault("decoded_timestamp_first_seconds", pts)
                state["decoded_timestamp_last_seconds"] = pts
                state["decoded_timestamp_max_gap_seconds"] = max(state.get("decoded_timestamp_max_gap_seconds", 0),
                    pts - previous_pts if previous_pts is not None else 0)
                if pts - state["decoded_timestamp_first_seconds"] > MAX_DURATION:
                    raise MediaError("Decoded timestamp duration exceeds the 30 minute limit. Partial results retained.")
            if image.size == 0 or image.shape[0] * image.shape[1] > MAX_PIXELS:
                raise MediaError("Invalid frame or changing resolution exceeds limits. Partial results retained.")
            state["decoded_frames"] += 1
            if frame_index != next_frame:
                continue
            if _cancel.is_set():
                break
            # In-flight inference finishes safely; its complete frame is saved.
            scan_start = scan_clock()
            faces = detect(image)
            matches = index.match_batch(np.stack([f.embedding for f in faces]), state["model"]["threshold"]) if faces else []
            if len(matches) != len(faces):
                raise RuntimeError("Matcher result count mismatch")
            work_seconds = max(0.0, scan_clock() - scan_start)
            state["scan_work_seconds"] += work_seconds
            timestamp = round(frame_index / state["media"]["fps"], 6)
            seen, unknown = {}, 0
            for match in matches:
                if match.person_id is None:
                    unknown += 1
                else:
                    seen[match.person_id] = match.full_name or match.first_name or "Unnamed identity"
            for person_id, name in seen.items():
                row = counts.setdefault(person_id, dict(person_id=person_id, name=name, detection_count=0,
                    first_timestamp_seconds=timestamp, last_timestamp_seconds=timestamp))
                row["detection_count"] += 1
                row["last_timestamp_seconds"] = timestamp
            state["sampled_frames"] += 1
            state["unknown_detections"] += unknown
            state["frames_with_unknown"] += int(unknown > 0)
            state["matched_face_detections"] += len(matches) - unknown
            state["people"] = sorted(counts.values(), key=lambda r: (-r["detection_count"], r["person_id"]))
            state["samples"].append(dict(frame_index=frame_index, timestamp_seconds=timestamp,
                faces=len(faces), matched_people=len(seen), unknown_detections=unknown,
                capture_time_seconds=round(capture_time, 9), scan_work_seconds=round(work_seconds, 9)))
            # Count all matches above; persist only one deterministic example
            # for each identity, reusing this detection's bbox and matcher score.
            for face, match in zip(faces, matches):
                if match.person_id is not None:
                    save_representative(state, counts[match.person_id], face, match, image, frame_index, timestamp)
            next_frame, capture_time = next_capture(frame_index, capture_time, work_seconds, state["media"]["fps"], settings)
            _checkpoint(state, clock)
        state["status"] = "cancelled" if _cancel.is_set() else "completed"
        if state["status"] == "completed":
            state["actual_max_samples_zero_work"] = maximum_sample_count(state["decoded_frames"], state["media"]["fps"], settings)
    except MediaError as exc:
        state.update(status="failed", error=str(exc))
    except Exception:
        logger.exception("Local video experiment failed")
        state.update(status="failed", error="Video processing failed. Partial results retained. Check backend log and try a supported video.")
    finally:
        if cap is not None:
            cap.release()
        with _lock:
            state.update(finished_at=now(), processing_seconds=round(time.perf_counter() - clock, 6), decode_phase="stopped")
            try:
                _write(state)
            finally:
                _active = None

def public(state: dict, *, detail: bool = True) -> dict:
    result = copy.deepcopy(state)
    result["can_start"] = state["status"] == "ready"
    result["can_cancel"] = state["status"] in ACTIVE
    result["can_delete"] = state["status"] not in ACTIVE | {"uploading"}
    result["partial"] = state["status"] in {"running", "cancelling", "cancelled", "interrupted", "failed"}
    result["unique_matched_people"] = len(state["people"])
    result["decoded_frames_not_inferred"] = max(0, state["decoded_frames"] - state["sampled_frames"])
    samples = state["samples"]
    # Inter-capture cadence avoids the first-frame boundary bias of N/duration.
    span = (samples[-1].get("capture_time_seconds", samples[-1]["timestamp_seconds"])
            - samples[0].get("capture_time_seconds", samples[0]["timestamp_seconds"])) if len(samples) > 1 else 0.0
    result["actual_video_detection_hz"] = round((len(samples) - 1) / span, 6) if span > 0 else None
    elapsed = state["processing_seconds"]
    result["processing_throughput_fps"] = round(state["sampled_frames"] / elapsed, 6) if elapsed > 0 else None
    selected = state.get("scan_settings")
    result["max_scan_hz"] = (min(selected["target_detections_per_second"], 1 / selected["post_scan_delay_seconds"])
                            if selected and selected["post_scan_delay_seconds"] > 0 else
                            selected["target_detections_per_second"] if selected else
                            MAX_SCAN_HZ if state["sampling_hz"] is None else state["sampling_hz"])
    audit = state.get("decode_audit") or {}
    result["verified_total_frames"] = audit.get("verified_frame_count") if audit.get("outcome") == "normal_eof" else None
    result["detected_face_detections"] = state["matched_face_detections"] + state["unknown_detections"]
    result["progress_is_estimate"] = result["verified_total_frames"] is None
    result["progress_percent"] = (100.0 if state["status"] == "completed" else
        round(min(99.9, 100 * state["decoded_frames"] / max(1, (state["media"] or {}).get("frame_count", 0))), 2))
    reviews = _reviews(state["id"]) if directory(state["id"]).exists() else {}
    for person in result["people"]:
        key = identity_key(state["id"], person.pop("person_id"))
        person["identity_key"] = key
        example = person.get("preview")
        person["preview_available"] = bool(example and preview_path(state["id"], key).is_file())
        review = reviews.get(key, {})
        person["manual_verdict"] = review.get("verdict", "Not reviewed") if example and review.get("example_key") == example.get("example_key") else "Not reviewed"
        person["preview_message"] = ("No valid representative frame is available for this match." if state.get("preview_version") else "Preview unavailable for this older run")
        person["review_meaning"] = "shown example only; does not verify every detection"
    if not detail:
        result.pop("samples")
        result.pop("people")
    return result

def _csv_text(value) -> str:
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text

def to_csv(state: dict) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["record_type", "key", "value", "identity_key", "name", "detection_count",
        "first_timestamp_seconds", "last_timestamp_seconds", "manual_verdict", "review_meaning",
        "preview_frame_number", "preview_timestamp_seconds", "preview_match_score"])
    model, media = state["model"] or {}, state["media"] or {}
    visible = public(state)
    metadata = {"experiment_id": state["id"], "status": state["status"], "partial_results": public(state)["partial"],
        "filename": state["filename"], "video_sha256": media.get("sha256", ""),
        "video_duration_seconds_nominal": media.get("duration_seconds", ""), "video_fps": media.get("fps", ""),
        "video_frame_count": media.get("frame_count", ""),
        "reported_frame_count_estimate": media.get("frame_count", ""),
        "verified_total_frames": visible["verified_total_frames"] if visible["verified_total_frames"] is not None else "",
        "decode_outcome": (state.get("decode_audit") or {}).get("outcome", "not_verified_in_original_run"),
        "decode_verifier": (state.get("decode_audit") or {}).get("verifier", ""),
        "decoded_timestamp_first_seconds": state.get("decoded_timestamp_first_seconds", ""),
        "decoded_timestamp_last_seconds": state.get("decoded_timestamp_last_seconds", ""),
        "decoded_timestamp_max_gap_seconds": state.get("decoded_timestamp_max_gap_seconds", ""),
        "decoded_nominal_duration_seconds": state["decoded_frames"] / media["fps"] if media.get("fps") else "",
        "actual_max_samples_zero_work": state.get("actual_max_samples_zero_work", ""),
        "detected_face_detections": visible["detected_face_detections"], "decoder": media.get("decoder", ""),
        "opencv_version": media.get("opencv_version", ""), "sampling_hz": state["sampling_hz"] if state["sampling_hz"] is not None else "variable",
        "sampling_method": state["sampling_method"], "post_scan_delay_seconds": state.get("post_scan_delay_seconds", ""),
        "camera_fps": (state.get("scan_settings") or {}).get("camera_fps", ""),
        "target_detections_per_second": (state.get("scan_settings") or {}).get("target_detections_per_second", ""),
        "effective_available_fps_limit": media.get("effective_available_fps_limit", ""),
        "historical_reference": state.get("historical_reference", ""), "timing_differences": state.get("timing_differences", ""),
        "max_scan_hz": visible["max_scan_hz"],
        "actual_video_detection_hz": visible["actual_video_detection_hz"] if visible["actual_video_detection_hz"] is not None else "",
        "processing_throughput_fps": visible["processing_throughput_fps"] if visible["processing_throughput_fps"] is not None else "",
        "decoded_frames_not_inferred": visible["decoded_frames_not_inferred"], "scan_work_seconds": state.get("scan_work_seconds", ""),
        "max_sample_count_zero_work": media.get("max_samples", ""),
        "sampled_frame_count": state["sampled_frames"], "decoded_frame_count": state["decoded_frames"],
        "processing_seconds": state["processing_seconds"], "model": model.get("name", ""),
        "provider": model.get("provider", ""), "detector_provider": model.get("detector_provider", ""),
        "match_threshold": model.get("threshold", ""), "detection_threshold": model.get("detection_threshold", ""),
        "detection_size": "x".join(map(str, model.get("detection_size", []))),
        "enrolled_identities": model.get("enrolled_identities", ""), "unique_matched_people": len(state["people"]),
        "matched_face_detections": state["matched_face_detections"], "unknown_face_detections": state["unknown_detections"],
        "frames_with_unknown": state["frames_with_unknown"], "started_at": state["started_at"] or "",
        "finished_at": state["finished_at"] or "", "error": state["error"] or "",
        "review_meaning": "manual verdict applies only to one shown representative example; no whole-run accuracy",
        "count_meaning": "detection_count = sampled frames containing identity, at most once per frame; no passage count or accuracy"}
    for key, value in metadata.items():
        writer.writerow(["metadata", key, _csv_text(value), "", "", "", "", "", "", "", "", "", ""])
    for row in public(state)["people"]:
        writer.writerow(["person", "", "", row["identity_key"], _csv_text(row["name"]), row["detection_count"],
            row["first_timestamp_seconds"], row["last_timestamp_seconds"], row["manual_verdict"], row["review_meaning"],
            (row.get("preview") or {}).get("frame_number", ""), (row.get("preview") or {}).get("timestamp_seconds", ""),
            (row.get("preview") or {}).get("match_score", "") if (row.get("preview") or {}).get("match_score") is not None else ""])
    return output.getvalue()
