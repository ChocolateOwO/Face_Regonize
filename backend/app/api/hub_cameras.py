"""Hub cameras for the CCTV station (see services/hub_camera.py).

    GET /api/hub/cameras            the Hub's camera list, or enabled=false
    GET /api/hub/cam.stream?dev=…   that camera's MJPEG, relayed unchanged

Signed-in human accounts only (GET: stations included, as for every read);
machine accounts are refused by get_current_user. Nothing is stored.
"""
from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.auth.deps import get_current_user
from app.models.models import User
from app.services import hub_camera, hub_link

router = APIRouter(prefix="/api/hub", tags=["hub"])

BOUNDARY = "reconizehub"


@router.get("/cameras")
def hub_cameras(user: User = Depends(get_current_user)):
    link = hub_link.status()
    if not link["configured"]:
        return {"enabled": False, "reason": link["reason"], "cameras": []}
    try:
        return {"enabled": True, "reason": "", "cameras": hub_camera.list_cameras()}
    except hub_camera.HubCameraError as exc:
        return {"enabled": True, "reason": str(exc), "cameras": []}


@router.get("/cam.stream")
async def hub_camera_stream(dev: str, request: Request, user: User = Depends(get_current_user)):
    try:
        resp = await anyio.to_thread.run_sync(hub_camera.open_stream, dev)
    except hub_camera.HubCameraError as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(exc.status, str(exc), headers=headers) from exc

    frames = hub_camera.frames(resp)

    def next_frame():
        return next(frames, None)

    async def body():
        try:
            while True:
                frame = await anyio.to_thread.run_sync(next_frame)
                if frame is None or await request.is_disconnected():
                    return
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
                       ).encode() + frame + b"\r\n"
        finally:
            frames.close()  # closes the Hub connection too

    return StreamingResponse(body(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                             headers={"Cache-Control": "no-store"})
