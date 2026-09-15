"""Phase F1 — admin controls for the evaluation-candidate backfill.

Returns job state and COUNTS only. No route here returns an embedding, a
face crop, an image path or any identity row — candidates are evaluation data
read server-side by the evaluation CLI, never through the web app.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.deps import get_current_user
from app.models.models import User
from app.services import identity_backfill_service as backfill

router = APIRouter(prefix="/api/identity-candidates", tags=["identity-candidates"])


class BackfillBody(BaseModel):
    batch_id: str | None = None
    max_photos: int = backfill.MAX_PHOTOS_DEFAULT


@router.get("/summary")
def candidate_summary(user: User = Depends(get_current_user)):
    return backfill.summary()


@router.get("/backfill")
def backfill_status(user: User = Depends(get_current_user)):
    return backfill.status()


@router.post("/backfill")
def start_backfill(body: BackfillBody, user: User = Depends(get_current_user)):
    try:
        return backfill.start(batch_id=body.batch_id, max_photos=body.max_photos)
    except backfill.BackfillRefused as e:
        raise HTTPException(409, str(e))


@router.post("/backfill/cancel")
def cancel_backfill(user: User = Depends(get_current_user)):
    return backfill.cancel()
