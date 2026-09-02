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
    return FileResponse(resolved)
