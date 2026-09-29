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

router = APIRouter(prefix="/api/local-video-experiment", tags=["local-video-experiment"], route_class=ScanSettingsRoute)

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
        "limits": {"max_bytes": video.MAX_BYTES, "max_duration_seconds": video.MAX_DURATION, "sampling_hz": video.SAMPLE_HZ,
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
        if length is not None and int(length) > video.MAX_BYTES:
            raise HTTPException(413, "Video exceeds 512 MiB upload limit.")
    except video.MediaError as exc:
        raise HTTPException(400, str(exc))
    state = video.allocate(filename)
    size, digest = 0, hashlib.sha256()
    try:
        with video.video_path(state).open("xb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > video.MAX_BYTES:
                    raise HTTPException(413, "Video exceeds 512 MiB upload limit.")
                digest.update(chunk)
                await run_in_threadpool(output.write, chunk)
        if size == 0 or (length is not None and size != int(length)):
            raise video.MediaError("Upload was empty or incomplete. Select video and upload again.")
        media = await run_in_threadpool(video.probe, video.video_path(state))
        return video.public(video.complete_upload(state["id"], size, digest.hexdigest(), media))
    except video.MediaError as exc:
        raise HTTPException(400, str(exc))
    except ClientDisconnect:
        raise HTTPException(400, "Upload disconnected. Select video and upload again.")
    finally:
        if video.get(state["id"])["status"] == "uploading":
            await run_in_threadpool(video.discard_upload, state["id"])

@router.get("/{job_id}")
def detail(job_id: str, user: User = Depends(require_admin)):
    return video.public(load(job_id))

@router.post("/{job_id}/start")
def start(job_id: str, body: ScanSettings | None = Body(default=None), user: User = Depends(require_admin)):
    load(job_id)
    try:
        return video.public(video.start(job_id, body))
    except video.Busy as exc:
        raise HTTPException(409, str(exc))

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
