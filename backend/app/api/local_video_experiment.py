"""Admin-only raw streamed video upload; no multipart spool."""
from __future__ import annotations
import hashlib
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from app.auth.deps import get_current_user
from app.models.models import User
from app.services import local_video_experiment as video
from app.services.scan_settings import ScanSettings, RANGES
from app.api.scan_settings import ScanSettingsRoute
from app.services import video_scheduler, video_devices
from app.services import video_render, video_render_media, video_render_overlay

router = APIRouter(prefix="/api/local-video-experiment", tags=["local-video-experiment"], route_class=ScanSettingsRoute)
UPLOAD_CHECK_BYTES = 8 * 1024 * 1024

def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user

def load(job_id: str) -> dict:
    try:
        return video.get(job_id)
    except video.NotFound:
        raise HTTPException(404, "Experiment not found")

@router.get("")
def list_experiments(user: User = Depends(require_admin)):
    return {"experiments": [video.public(s, detail=False) for s in video.list_jobs()],
        "scheduler": video_scheduler.telemetry(),
        "limits": {"disk_reserve_bytes": video.DISK_RESERVE, "max_duration_seconds": video.MAX_DURATION, "sampling_hz": video.SAMPLE_HZ,
            "post_scan_delay_seconds": video.POST_SCAN_DELAY_SECONDS, "max_scan_hz": video.MAX_SCAN_HZ,
            "scan_settings_defaults": ScanSettings().model_dump(), "scan_settings_ranges": RANGES}}

@router.post("", status_code=201)
async def upload(request: Request, filename: str, user: User = Depends(require_admin)):
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/octet-stream":
        raise HTTPException(415, "Send video file bytes as application/octet-stream.")
    try:
        video.filename(filename)
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) <= 0):
            raise video.MediaError("Empty or invalid upload length.")
        declared = int(length) if length is not None else None
    except video.MediaError as exc:
        raise HTTPException(400, str(exc))
    claim = ("upload", object())
    try:
        # No fixed size ceiling: the declared size must fit the shared budget.
        video.claim_disk(claim, declared or 0, filename)
    except video.MediaError as exc:
        raise HTTPException(507, str(exc))
    try:
        state = video.allocate(filename)
    except BaseException:
        video.release_disk(claim)
        raise
    size, digest, next_check = 0, hashlib.sha256(), 0
    try:
        with video.video_path(state).open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if declared is not None and size > declared:
                    raise video.MediaError("Upload sent more bytes than declared. Select video and upload again.")
                if size >= next_check:
                    # Recheck the budget every 8 MiB: the bytes still to come
                    # (this chunk included) must still fit beside other videos.
                    next_check = size + UPLOAD_CHECK_BYTES
                    remaining = (declared - size + len(chunk)) if declared is not None else UPLOAD_CHECK_BYTES
                    try:
                        await run_in_threadpool(video.claim_disk, claim, remaining, filename)
                    except video.MediaError as exc:
                        raise HTTPException(507, str(exc))
                digest.update(chunk)
                await run_in_threadpool(output.write, chunk)
        video.release_disk(claim)
        if size == 0 or (length is not None and size != int(length)):
            raise video.MediaError("Upload was empty or incomplete. Select video and upload again.")
        media = await run_in_threadpool(video.probe, video.video_path(state))
        return video.public(video.complete_upload(state["id"], size, digest.hexdigest(), media))
    except video.MediaError as exc:
        raise HTTPException(400, str(exc))
    except ClientDisconnect:
        raise HTTPException(400, "Upload disconnected. Select video and upload again.")
    finally:
        video.release_disk(claim)
        if video.get(state["id"])["status"] == "uploading":
            await run_in_threadpool(video.discard_upload, state["id"])

@router.get("/execution")
def execution(user: User = Depends(require_admin)):
    return {**video_devices.capabilities(), **video_scheduler.telemetry()}

@router.post("/execution")
def configure_execution(body: video_scheduler.WorkerSettings, user: User = Depends(require_admin)):
    return video_scheduler.configure(body)

def _modes(key: str, gpu: dict, gpu_name: str, cpu_name: str, auto: str) -> list[dict]:
    ok, reason = gpu[key]
    return [{"value": "auto", "label": "Auto", "available": True, "detail": auto},
            {"value": "gpu", "label": f"GPU ({gpu_name})", "available": ok, "detail": None if ok else reason},
            {"value": "cpu", "label": f"CPU ({cpu_name})", "available": True, "detail": None}]

@router.get("/render/capabilities")
def render_capabilities(user: User = Depends(require_admin)):
    """What a tagged-video render can use on this machine right now."""
    fonts = video_render_overlay.load_fonts()
    ffmpeg = bool(video_render_media.find_tool("ffmpeg") and video_render_media.find_tool("ffprobe"))
    gpu = video_render_media.gpu_modes()
    return {"available": ffmpeg, "ffmpeg_found": ffmpeg,
        "message": None if ffmpeg else "FFmpeg (ffmpeg and ffprobe) was not found. Install FFmpeg or set RECONIZE_FFMPEG to render tagged videos.",
        "detectors": [{"value": key, "label": label} for key, label in video_render.DETECTORS.items()],
        "default_detector": "plain", "font": fonts.description, "thai_labels": fonts.thai,
        "decoder_modes": _modes("decoder", gpu, "NVDEC", "OpenCV FFmpeg", "CPU: measured no slower than NVDEC, and it uses no extra VRAM"),
        "encoder_modes": _modes("encoder", gpu, "h264_nvenc", "libx264",
                                video_render_media.ENCODER_LABELS[video_render_media.ENCODER_ORDER[0]] + " first, then "
                                + video_render_media.ENCODER_LABELS[video_render_media.ENCODER_ORDER[1]] + " if it cannot encode that video"),
        "mode_note": "GPU runs on the GPU or the render stops with the reason; it never falls back to the CPU. Auto picks the faster measured path.",
        "timing": "source timestamps are preserved; no frames are duplicated or dropped",
        "panel_width": video_render_overlay.PANEL_W}

@router.get("/{job_id}")
def detail(job_id: str, user: User = Depends(require_admin)):
    return video.public(load(job_id))

@router.post("/{job_id}/start")
def start(job_id: str, body: video_devices.VideoSettings | None = Body(default=None), user: User = Depends(require_admin)):
    load(job_id)
    try:
        body = body or video_devices.VideoSettings()
        return video.public(video.start(job_id, ScanSettings.model_validate(body.model_dump(exclude={"device", "render"})), body.device,
                                        body.render.model_dump() if body.render else None))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
    except (video_devices.DeviceError, video.MediaError) as exc:
        raise HTTPException(400, str(exc))

@router.post("/{job_id}/cancel")
def cancel(job_id: str, user: User = Depends(require_admin)):
    load(job_id)
    return video.public(video.cancel(job_id))

@router.delete("/{job_id}")
def delete(job_id: str, user: User = Depends(require_admin)):
    load(job_id)
    try:
        video.remove(job_id)
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
    return {"deleted": True}

@router.get("/{job_id}/video")
def download_video(job_id: str, user: User = Depends(require_admin)):
    state = load(job_id)
    if state.get("kind") == "drive_batch":
        raise HTTPException(410, "Drive videos are temporary input and are removed locally after processing. Original Drive files are unchanged.")
    path = video.video_path(state)
    if not path.is_file():
        raise HTTPException(404, "Experiment video not found")
    return FileResponse(path, filename=state["filename"], media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

def _render_record(state: dict, source_key: str) -> dict:
    source = video_scheduler.render_entry(state, source_key)
    record = (source or {}).get("render")
    if not record:
        raise HTTPException(404, "No tagged video for this source.")
    return {"source": source, "record": record}

@router.post("/{job_id}/render")
def render_later(job_id: str, body: video_render.RenderSettings, user: User = Depends(require_admin)):
    """Render the tagged video of a retained LOCAL upload after its run."""
    load(job_id)
    try:
        settings = {**body.model_dump(), "enabled": True}
        return video.public(video_scheduler.request_render(job_id, settings))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
    except (video.MediaError, video.NotFound) as exc:
        raise HTTPException(400, str(exc))

@router.post("/{job_id}/sources/{source_key}/render/cancel")
def render_cancel(job_id: str, source_key: str, user: User = Depends(require_admin)):
    _render_record(load(job_id), source_key)
    return video.public(video_scheduler.cancel_render(job_id, source_key))

@router.delete("/{job_id}/sources/{source_key}/render")
def render_delete(job_id: str, source_key: str, user: User = Depends(require_admin)):
    _render_record(load(job_id), source_key)
    try:
        return video.public(video_scheduler.delete_render(job_id, source_key))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
    except video.NotFound as exc:
        raise HTTPException(404, str(exc))

@router.get("/{job_id}/sources/{source_key}/render.mp4")
def render_download(job_id: str, source_key: str, user: User = Depends(require_admin)):
    """One tagged video per source: a Drive batch of seven videos has seven
    separate links (video-001 ... video-007); a local upload uses 'local'."""
    found = _render_record(load(job_id), source_key)
    if found["record"]["status"] != "completed":
        raise HTTPException(409, "The tagged video for this source is not finished.")
    try:
        path = video_render.output_path(job_id, source_key)
    except (video.MediaError, video.NotFound):
        raise HTTPException(404, "Tagged video not found")
    if not path.is_file():
        raise HTTPException(404, "Tagged video not found")
    stem = found["source"].get("filename", source_key).rsplit(".", 1)[0] or source_key
    return FileResponse(path, filename=f"{stem}_tagged.mp4", media_type="video/mp4",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

@router.get("/{job_id}/report.csv")
def report(job_id: str, user: User = Depends(require_admin)):
    return Response(video.to_csv(load(job_id)).encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="local-video-{job_id}.csv"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


class ExampleReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["Not reviewed", "Correct", "Incorrect", "Unsure"]
    example_key: str = Field(pattern=r"^[0-9a-f]{32}$")


@router.get("/{job_id}/people/{key}/preview")
def preview(job_id: str, key: str, example_key: str, user: User = Depends(require_admin)):
    state = load(job_id)
    if state["status"] in video.ACTIVE:
        raise HTTPException(409, "Wait for processing to stop before opening its final example.")
    person = next((p for p in video.public(state)["people"] if p["identity_key"] == key), None)
    if not person or not person["preview_available"]:
        raise HTTPException(404, "Preview unavailable for this older run")
    if person["preview"]["example_key"] != example_key:
        raise HTTPException(409, "Shown example changed. Open the current match preview.")
    path = video.preview_path(job_id, key)
    if hashlib.sha256(path.read_bytes()).hexdigest() != person["preview"].get("image_sha256"):
        raise HTTPException(409, "Saved preview is incomplete after interruption; no video will be reprocessed automatically.")
    return FileResponse(path, media_type="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/{job_id}/people/{key}/review")
def review(job_id: str, key: str, body: ExampleReview, user: User = Depends(require_admin)):
    try:
        return video.review_example(job_id, key, body.verdict, body.example_key)
    except video.NotFound as exc:
        raise HTTPException(404, str(exc))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))
