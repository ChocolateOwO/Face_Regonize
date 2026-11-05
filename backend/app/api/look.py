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
LOOK_BATCH_PATH = "/api/look/batch"
_SOURCE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class LookBusy(HTTPException):
    """A 429 from the look endpoints. Still an HTTPException with the same
    detail text and Retry-After/X-Retry-After-Ms headers as before; the
    handler in main.py adds limit/reason/retry_after_ms to the JSON body."""

    def __init__(self, exc: look_service.LookError):
        super().__init__(429, exc.detail, headers=exc.headers)
        self.limit = exc.limit
        self.reason = exc.reason
        self.retry_after_ms = exc.retry_after_ms

    def body(self) -> dict:
        return {"detail": self.detail, "limit": self.limit, "reason": self.reason,
                "retry_after_ms": self.retry_after_ms}


def _http_error(exc: look_service.LookError) -> HTTPException:
    return LookBusy(exc) if exc.status == 429 else HTTPException(exc.status, exc.detail, headers=exc.headers)


@router.post("/look")
def look(
    photo: UploadFile = File(..., description="JPEG frame, at most 2 MiB."),
    source: str | None = Form(None, description="Caller's camera label, e.g. nong-cam-1. Each source gets a fair "
                                                "turn on the live model; logged, never stored."),
    want: Literal["faces", "persons"] = Form("faces", description="'persons' is reserved and not implemented."),
    user: User = Depends(get_api_client),
):
    """Detect and identify every face in one JPEG. Nothing is saved, no one is
    checked in, nothing is published. Faces are sorted largest first; boxes
    are [x1, y1, x2, y2] normalized to 0-1. Unknown faces carry no identity.

    400 not a readable JPEG · 401 bad token · 413 too large (bytes or pixels)
    · 422 want=persons · 429 busy, drop this frame (Retry-After = estimated
    whole seconds, X-Retry-After-Ms = the estimate; body adds limit, reason,
    retry_after_ms) · 503 model unavailable."""
    if want == "persons":
        raise HTTPException(422, "want=persons is reserved for future body detection and is not available.")
    data = photo.file.read(look_service.MAX_PHOTO_BYTES + 1)
    try:
        result = look_service.look(data, source=source, user_id=getattr(user, "id", None))
    except look_service.LookError as exc:
        raise _http_error(exc) from exc
    if source:
        logger.debug("look source=%s faces=%d took_ms=%.1f",
                     _SOURCE_RE.sub("_", source)[:64], len(result["faces"]), result["took_ms"])
    return result


@router.post("/look/batch")
def look_batch(
    photos: list[UploadFile] = File(..., description=f"1-{look_service.MAX_BATCH} JPEG frames, each at most 2 MiB."),
    sources: list[str] | None = Form(None, description="One camera label per photo, in the same order (optional)."),
    user: User = Depends(get_api_client),
):
    """Several frames in one request, each from its own camera. One turn on
    the live model for the whole batch; all faces are embedded in one GPU
    batch. Per-item results have the /api/look shape plus index, source and
    status (200, 400, 413 or 429); one bad or busy item never fails the
    others. The whole request is 429 only when every item was refused as
    busy, 422 when the batch itself is malformed. Nothing is saved."""
    if not 1 <= len(photos) <= look_service.MAX_BATCH:
        raise HTTPException(422, f"Send 1-{look_service.MAX_BATCH} photos per batch.")
    if sources is not None and len(sources) != len(photos):
        raise HTTPException(422, "sources must have one entry per photo, in the same order.")
    items = [(p.file.read(look_service.MAX_PHOTO_BYTES + 1), (sources[i] if sources else None) or None)
             for i, p in enumerate(photos)]
    try:
        return look_service.look_batch(items, user_id=getattr(user, "id", None))
    except look_service.LookError as exc:
        raise _http_error(exc) from exc


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
