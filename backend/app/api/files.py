from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import STORAGE_PATH
from app.services.photo_thumbnail_service import ensure_thumbnail, is_thumbnail_path

router = APIRouter(prefix="/api/files", tags=["files"])

# A participant photo lives at a fixed url (people/{id}/profile.jpg) and is
# overwritten in place when replaced - and participant ids get reused. Without
# revalidation the browser may serve its cached copy without asking, showing
# the previous occupant's face beside the new name. "no-cache" does not forbid
# caching, only using a copy that has not been revalidated. This is the
# default for everything.
_REVALIDATE = "no-cache, must-revalidate"

# Grid thumbnails are the one exception. They are requested 50-100 at a time
# on every page change, and their URLs carry an ?v=<mtime> stamp (see
# photo_thumbnail_service.versioned), so a RE-RENDERED photo asks for a
# different URL rather than reusing a tile that may show a face the new render
# masks. Only these may be cached without revalidating.
_IMMUTABLE = "private, max-age=31536000, immutable"


def _cache_control(path: str) -> str:
    return _IMMUTABLE if is_thumbnail_path(path) else _REVALIDATE


@router.get("/{path:path}")
def get_file(path: str):
    resolved = (STORAGE_PATH / path).resolve()
    if not str(resolved).startswith(str(STORAGE_PATH.resolve())):
        raise HTTPException(400, "Invalid path")
    # A grid thumbnail is derived and disposable, so a batch that predates
    # thumbnails (or one whose tile was cleaned up) generates it here, on the
    # image request itself. Doing it per image rather than per page keeps the
    # photo-list response fast and lets the browser's parallel image fetches
    # spread the cost. The path has already been confined to STORAGE_PATH.
    if not resolved.is_file() and is_thumbnail_path(path):
        ensure_thumbnail(STORAGE_PATH, path)
        resolved = (STORAGE_PATH / path).resolve()
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(resolved, headers={"Cache-Control": _cache_control(path)})
