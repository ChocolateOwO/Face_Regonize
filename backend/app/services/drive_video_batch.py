"""Drive batch records for Local Video Experiment, never Event/Legacy Drive.

Bounded source workers managed by video_scheduler; one preview per identity.
Checkpoints contain source results. Aggregation is replacement, not increments;
terminal/interrupted batches never restart or retry their completed sources.
"""
from __future__ import annotations
import copy
import csv
import io
import json
import logging
import shutil
import time
from pathlib import Path
from app.services import local_video_experiment as video
from app.services import drive_video_source as drive
from app.services.scan_settings import ScanSettings

logger = logging.getLogger(__name__)
TOTAL_FIELDS = ("sampled_frames", "decoded_frames", "unknown_detections", "frames_with_unknown", "matched_face_detections", "scan_work_seconds")
TERMINAL_VIDEO = {"completed", "failed", "cancelled", "interrupted", "not_processed"}
COUNT_MEANING = "sum of source sampled-frame identity counts, at most once per identity per sampled frame per video; no cross-camera passage deduplication or accuracy"
TIME_MEANING = "timestamps are seconds relative to each source video, not a shared camera clock; batch first=min(source first), last=max(source last); see video_person rows"

def downloads_dir(job_id: str) -> Path:
    parent = video.directory(job_id)
    folder = parent / "downloads"
    if folder.is_symlink() or folder.resolve().parent != parent.resolve():
        raise video.MediaError("Temporary download storage is unavailable.")
    return folder

def cleanup(job_id: str) -> None:
    folder = downloads_dir(job_id)
    if folder.exists():
        # Explicitly confined to this job's generated downloads subdirectory.
        shutil.rmtree(folder)

def refresh_totals(state: dict) -> None:
    previous = {p["person_id"]: p for p in state["people"]}
    people = {}
    for source in state["videos"]:
        for row in source["people"]:
            key = row["person_id"]
            target = people.setdefault(key, dict(person_id=key, name=row["name"], detection_count=0,
                first_timestamp_seconds=row["first_timestamp_seconds"], last_timestamp_seconds=row["last_timestamp_seconds"]))
            target["detection_count"] += row["detection_count"]
            target["first_timestamp_seconds"] = min(target["first_timestamp_seconds"], row["first_timestamp_seconds"])
            target["last_timestamp_seconds"] = max(target["last_timestamp_seconds"], row["last_timestamp_seconds"])
    for key, row in people.items():
        if previous.get(key, {}).get("preview"):
            row["preview"] = previous[key]["preview"]
    state["people"] = sorted(people.values(), key=lambda p: (-p["detection_count"], p["person_id"]))
    for field in TOTAL_FIELDS:
        state[field] = sum(v.get(field, 0) for v in state["videos"])

def recover(state: dict) -> None:
    """Called at startup even for terminal batches, cleaning stale downloads.

    No network or model call; no result recalculation for an older single run.
    Active sources become interrupted. Start rejects the resulting batch.
    """
    try:
        cleanup(state["id"])
        state["temporary_downloads_cleaned"] = True
    except Exception:
        state.update(status="failed", finished_at=video.now(), temporary_downloads_cleaned=False,
            error="Restart cleanup could not remove temporary downloads. Check storage permissions; Delete this stopped experiment to remove its local files.")
        video._write(state)
        return
    if state["status"] in video.ACTIVE:
        for source in state["videos"]:
            if source["status"] in {"downloading", "running"}:
                source.update(status="interrupted", error="Backend restarted; partial results retained, temporary source removed.", finished_at=video.now())
            elif source["status"] == "pending":
                source.update(status="not_processed", error="Not started before backend restart.")
        refresh_totals(state)
    video._write(state)

def create(name: str, link: str, keys: list[str], account_id: str, settings: ScanSettings, device: str = "auto") -> dict:
    label = name.strip()
    if not label or len(label) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise drive.DriveVideoError("Give the batch a name of 1–120 characters without control characters.")
    parent, account, selected = drive.selected_files(link, keys, account_id)
    with video._lock:
        state = video.allocate("drive-batch.mp4")
        state.update(kind="drive_batch", filename=label, batch_name=label, status="ready",
            folder_id=parent, drive_account=account, selected_files=copy.deepcopy(selected),
            input_limits=drive.limits(),
            temporary_downloads_cleaned=True, count_meaning=COUNT_MEANING, timestamp_meaning=TIME_MEANING,
            videos=[], current_video_key=None)
        for order, item in enumerate(selected):
            source = {k: copy.deepcopy(v) for k, v in state.items() if k not in {
                "videos", "selected_files", "drive_account", "folder_id", "kind", "batch_name", "count_meaning", "timestamp_meaning", "current_video_key"}}
            source.update(filename=item["filename"], source_video_key=f"video-{order + 1:03d}",
                source_order=order, status="pending", downloaded_bytes=0, scan_settings=settings.model_dump(),
                source_file=copy.deepcopy(item), preview_version=1)
            state["videos"].append(source)
        video._write(state)
        return video.start(state["id"], settings, device)

def decorate_public(result: dict, state: dict, *, detail: bool) -> None:
    sources = [video.public(s, detail=detail) for s in state["videos"]]
    for source in sources:
        source.pop("source_file", None)  # Private Drive resource keys/version checks.
    result["videos"] = sources
    result.pop("selected_files", None)
    result["total_videos"] = len(sources)
    result["completed_videos"] = sum(s["status"] == "completed" for s in sources)
    result["failed_videos"] = sum(s["status"] == "failed" for s in sources)
    result["finished_videos"] = sum(s["status"] in TERMINAL_VIDEO for s in sources)
    # Aggregate cadence is completed sample intervals / sum of their source
    # virtual-time spans, never a guessed shared camera timeline.
    intervals, span = 0, 0.0
    for source in state["videos"]:
        samples = source["samples"]
        if len(samples) > 1:
            intervals += len(samples) - 1
            span += samples[-1].get("capture_time_seconds", samples[-1]["timestamp_seconds"]) - samples[0].get("capture_time_seconds", samples[0]["timestamp_seconds"])
    result["actual_video_detection_hz"] = round(intervals / span, 6) if span > 0 else None
    result["progress_percent"] = round(sum(1 if s["status"] in TERMINAL_VIDEO else
        0.5 * min(1, s.get("downloaded_bytes", 0) / max(1, s["source_file"]["bytes"])) if s["status"] == "downloading" else
        0.5 + 0.5 * min(0.999, s["decoded_frames"] / max(1, (s["media"] or {}).get("frame_count", 0))) if s["status"] == "running" else 0
        for s in state["videos"]) * 100 / max(1, len(sources)), 2)
    result["progress_is_estimate"] = state["status"] in video.ACTIVE
    result["count_meaning"] = COUNT_MEANING
    result["timestamp_meaning"] = TIME_MEANING
    if not detail:
        result.pop("videos")

def to_csv(state: dict) -> str:
    """Original 13 columns preserved; source fields appended for batches only.

    metadata=key/value, person=combined count/review, video=source stats,
    video_person=one identity/source. Never sum person and video_person rows.
    """
    visible = video.public(state)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    headers = ["record_type", "key", "value", "identity_key", "name", "detection_count", "first_timestamp_seconds", "last_timestamp_seconds",
        "manual_verdict", "review_meaning", "preview_frame_number", "preview_timestamp_seconds", "preview_match_score",
        "source_video_key", "source_filename", "status", "source_fps", "reported_frame_count_estimate", "decoded_frame_count", "verified_total_frames",
        "sampled_frame_count", "detected_face_detections", "matched_face_detections", "unknown_face_detections", "processing_seconds",
        "actual_video_detection_hz", "duration_seconds_nominal", "error", "requested_provider", "actual_provider", "stage_seconds"]
    writer.writerow(headers)
    def write(**fields):
        writer.writerow([video._csv_text(fields.get(key, "")) if fields.get(key) is not None else "" for key in headers])
    model = state["model"] or {}
    metadata = dict(experiment_id=state["id"], experiment_kind="drive_batch", batch_name=state["batch_name"], status=state["status"],
        partial_results=visible["partial"], total_videos=visible["total_videos"], completed_videos=visible["completed_videos"], failed_videos=visible["failed_videos"],
        sampling_method=state["sampling_method"], **(state.get("scan_settings") or {}), historical_reference=state["historical_reference"],
        timing_differences=state["timing_differences"], sampled_frame_count=state["sampled_frames"], decoded_frame_count=state["decoded_frames"],
        detected_face_detections=visible["detected_face_detections"], matched_face_detections=state["matched_face_detections"],
        unknown_face_detections=state["unknown_detections"], frames_with_unknown=state["frames_with_unknown"], unique_matched_people=len(state["people"]),
        processing_seconds=state["processing_seconds"], processing_throughput_fps=visible["processing_throughput_fps"],
        actual_video_detection_hz=visible["actual_video_detection_hz"], model=model.get("name"), provider=model.get("provider"),
        detector_provider=model.get("detector_provider"), match_threshold=model.get("threshold"), detection_threshold=model.get("detection_threshold"),
        detection_size="x".join(map(str, model.get("detection_size", []))), enrolled_identities=model.get("enrolled_identities"),
        count_meaning=COUNT_MEANING, timestamp_meaning=TIME_MEANING,
        aggregate_rate_meaning="sum(source completed sample intervals) / sum(source virtual capture spans); not a concurrent camera rate",
        processing_time_scope="batch/source processing_seconds include download, media validation, recognition, checkpoints and EOF verification; offline throughput includes these costs",
        review_meaning="manual verdict covers the one shown example only; no whole-run accuracy", temporary_downloads_cleaned=state["temporary_downloads_cleaned"],
        source_order="filename casefold, then Drive file ID; earliest source order and frame break equal-score ties",
        started_at=state["started_at"], finished_at=state["finished_at"], error=state["error"])
    if state.get("input_limits"):
        limits = state["input_limits"]
        if "max_bytes" in limits:  # batches created under the former 16 GiB ceiling
            metadata.update(input_max_bytes=limits["max_bytes"])
        else:
            metadata.update(input_size_limit="none per file; free disk space minus other active transfers minus "
                f"{limits.get('disk_reserve_bytes', 0)} reserve bytes")
        metadata.update(input_max_duration_seconds=limits["max_duration_seconds"])
    from app.services.video_scheduler import csv_metadata
    metadata.update(csv_metadata(state))
    for key, value in metadata.items():
        write(record_type="metadata", key=key, value=value)
    for source in visible["videos"]:
        media = source["media"] or {}
        write(record_type="video", source_video_key=source["source_video_key"], source_filename=source["filename"], status=source["status"],
            source_fps=media.get("fps"), reported_frame_count_estimate=media.get("frame_count"), decoded_frame_count=source["decoded_frames"],
            verified_total_frames=source["verified_total_frames"], sampled_frame_count=source["sampled_frames"], detected_face_detections=source["detected_face_detections"],
            matched_face_detections=source["matched_face_detections"], unknown_face_detections=source["unknown_detections"], processing_seconds=source["processing_seconds"],
            actual_video_detection_hz=source["actual_video_detection_hz"], duration_seconds_nominal=media.get("duration_seconds"), error=source["error"],
            requested_provider=source.get("requested_provider"), actual_provider=source.get("actual_provider"), stage_seconds=json.dumps(source.get("stage_seconds", {}), sort_keys=True))
        for row in source["people"]:
            write(record_type="video_person", source_video_key=source["source_video_key"], source_filename=source["filename"],
                identity_key=row["identity_key"], name=row["name"], detection_count=row["detection_count"],
                first_timestamp_seconds=row["first_timestamp_seconds"], last_timestamp_seconds=row["last_timestamp_seconds"])
    for row in visible["people"]:
        example = row.get("preview") or {}
        write(record_type="person", identity_key=row["identity_key"], name=row["name"], detection_count=row["detection_count"],
            first_timestamp_seconds=row["first_timestamp_seconds"], last_timestamp_seconds=row["last_timestamp_seconds"],
            manual_verdict=row["manual_verdict"], review_meaning=row["review_meaning"], preview_frame_number=example.get("frame_number"),
            preview_timestamp_seconds=example.get("timestamp_seconds"), preview_match_score=example.get("match_score"),
            source_video_key=example.get("source_video_key"), source_filename=example.get("source_filename"))
    return output.getvalue()
