"""POST /api/look — stateless face recognition for one JPEG (robot hub).

See services/look_service.py for the contract. This module is only the HTTP
shape: form fields, status codes, and the raw-body byte cap that has to sit
in front of multipart parsing (Starlette spools file parts with no size
limit of its own, so a cap checked inside the handler would come too late).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth.deps import get_api_client
from app.models.models import User
from app.services import look_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["look"])

LOOK_PATH = "/api/look"
_SOURCE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@router.post("/look")
def look(
    photo: UploadFile = File(..., description="JPEG frame, at most 2 MiB."),
    source: str | None = Form(None, description="Caller's camera label, e.g. nong-cam-1. Logged only."),
    want: Literal["faces", "persons"] = Form("faces", description="'persons' is reserved and not implemented."),
    user: User = Depends(get_api_client),
):
    """Detect and identify every face in one JPEG. Nothing is saved, no one is
    checked in, nothing is published. Faces are sorted largest first; boxes
    are [x1, y1, x2, y2] normalized to 0-1. Unknown faces carry no identity.

    400 not a readable JPEG · 401 bad token · 413 too large (bytes or pixels)
    · 422 want=persons · 429 busy, drop this frame · 503 model unavailable."""
    if want == "persons":
        raise HTTPException(422, "want=persons is reserved for future body detection and is not available.")
    data = photo.file.read(look_service.MAX_PHOTO_BYTES + 1)
    try:
        result = look_service.look(data)
    except look_service.LookError as exc:
        raise HTTPException(exc.status, exc.detail, headers=exc.headers) from exc
    if source:
        logger.debug("look source=%s faces=%d took_ms=%.1f",
                     _SOURCE_RE.sub("_", source)[:64], len(result["faces"]), result["took_ms"])
    return result


class _BodyTooLarge(Exception):
    pass


class LookBodyLimit:
    """Pure ASGI guard for POST /api/look only: refuses a request whose body
    exceeds MAX_BODY_BYTES with 413, counting bytes as they arrive, so it also
    holds for chunked uploads without Content-Length. Every other path passes
    straight through untouched."""

    def __init__(self, app, limit: int = look_service.MAX_BODY_BYTES, path: str = LOOK_PATH):
        self.app = app
        self.limit = limit
        self.path = path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.limit
                except ValueError:
                    too_big = False
                if too_big:
                    await self._reject(send)
                    return
        state = {"received": 0, "over": False}

        async def limited_receive():
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > self.limit:
                    state["over"] = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            # Once over the limit, whatever the app tries to answer (FastAPI
            # turns the interrupted form parse into a 400) is replaced by 413.
            if not state["over"]:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if state["over"]:
            await self._reject(send)

    async def _reject(self, send):
        body = json.dumps({"detail": f"Request body exceeds {self.limit} bytes."}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
