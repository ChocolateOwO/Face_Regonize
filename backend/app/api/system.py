from __future__ import annotations

from fastapi import APIRouter, Depends

from sqlmodel import Session

from app.auth.deps import get_current_user, require_admin
from app.database.db import get_session
from app.models.models import User
from app.services.system_info import get_system_info

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
def system_info(user: User = Depends(get_current_user)):
    return get_system_info()


@router.get("/security")
def security_status(session: Session = Depends(get_session), user: User = Depends(require_admin)):
    """Admin only: whether this install still uses the public default JWT
    secret or admin password (yes/no only, never a value), and how to fix it."""
    from app.services import security_defaults

    result = security_defaults.check(session)
    return {**result, "fix": security_defaults.fix_steps(result)}
