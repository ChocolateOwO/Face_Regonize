from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.auth.security import FILE_SCOPE, decode_access_token
from app.database.db import get_session
from app.models.models import User

bearer_scheme = HTTPBearer(auto_error=False)

# A machine account (the robot hub/voice service) signs in like any user but
# may call ONLY the routes that depend on get_api_client() below. Everything
# else - every route that uses get_current_user, which is all of them unless
# deliberately switched - refuses it with 403. Deny by default: a route added
# later is closed to machines unless someone opts it in on purpose.
MACHINE_ROLE = "machine"
ADMIN_ROLE = "admin"

# Changing data is admin-only by default: get_current_user refuses these
# methods for any non-admin role. The routes a non-admin STATION account must
# still operate (kiosk scan/consent, CCTV node traffic) opt out explicitly via
# get_station_user. Today every human account is an admin, so this changes
# nothing for existing users; it closes the door for any other role.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _authenticate(credentials: HTTPAuthorizationCredentials | None, session: Session) -> User:
    if credentials is None:
        raise HTTPException(401, "Not authenticated")
    payload = decode_access_token(credentials.credentials)
    if payload is None or payload.get("scope") == FILE_SCOPE:
        raise HTTPException(401, "Invalid or expired token")
    user = session.get(User, payload.get("sub"))
    if user is None:
        raise HTTPException(401, "User not found")
    return user


def is_machine(user: User) -> bool:
    return user.role == MACHINE_ROLE


def _human(credentials: HTTPAuthorizationCredentials | None, session: Session) -> User:
    user = _authenticate(credentials, session)
    if is_machine(user):
        raise HTTPException(403, "This machine account may only use the robot integration endpoints.")
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Any human account may read; only an admin may change data."""
    user = _human(credentials, session)
    if request.method in MUTATING_METHODS and user.role != ADMIN_ROLE:
        raise HTTPException(403, "Admin access required to change data.")
    return user


def get_station_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Human accounts, including non-admin stations, for the operational
    writes a kiosk or CCTV station performs (scan, kiosk consent, node
    register/heartbeat/event/recording). Never machine accounts."""
    return _human(credentials, session)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def get_api_client(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_session),
) -> User:
    """Human users AND machine accounts. Only for the robot-integration
    allowlist (look, observe-only recognition, read-only history/dashboard/
    node status, filtered settings, /api/auth/me)."""
    return _authenticate(credentials, session)


def token_user(token: str | None, session: Session) -> User | None:
    """WebSocket variant (token in the query string): the signed-in user, or
    None for a missing/invalid/expired token or a deleted user."""
    if not token:
        return None
    payload = decode_access_token(token)
    if payload is None or payload.get("scope") == FILE_SCOPE:
        return None
    return session.get(User, payload.get("sub"))
