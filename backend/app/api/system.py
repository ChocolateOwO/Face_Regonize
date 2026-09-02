from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.models.models import User
from app.services.system_info import get_system_info

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
def system_info(user: User = Depends(get_current_user)):
    return get_system_info()
