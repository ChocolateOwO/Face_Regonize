from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import STORAGE_PATH

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{path:path}")
def get_file(path: str):
    resolved = (STORAGE_PATH / path).resolve()
    if not str(resolved).startswith(str(STORAGE_PATH.resolve())):
        raise HTTPException(400, "Invalid path")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(404, "File not found")
    # A participant photo lives at a fixed url (people/{id}/profile.jpg) and is
    # overwritten in place when replaced - and participant ids get reused. Without
    # this the browser may serve its cached copy without asking, showing the
    # previous occupant's face beside the new name. "no-cache" does not forbid
    # caching, only using a copy that has not been revalidated.
    return FileResponse(
        resolved,
        headers={"Cache-Control": "no-cache, must-revalidate"}
    )
