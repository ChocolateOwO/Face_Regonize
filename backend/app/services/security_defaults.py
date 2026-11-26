"""Is this install still using the public default credentials?

The repository is public, so the built-in defaults are known to everyone:
the JWT signing secret (anyone could forge a token for any account) and the
first admin password. Setup (scripts/init_env.py) replaces both on a new
install, but an install made before that may still run with them.

This only REPORTS the risk - startup log, the admin-only
/api/system/security endpoint and the admin banner - it never blocks
startup, so development and test fixtures keep working. Values are never
returned or logged, only yes/no.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

logger = logging.getLogger(__name__)

DEFAULT_JWT_SECRET = "dev-secret-change-me-in-production"
DEFAULT_ADMIN_PASSWORD = "admin123"

STEP_STOP = "Stop Reconize."
STEP_SECRET = (r"In .env set JWT_SECRET to a long random value "
               r"(for example: backend\.venv\Scripts\python.exe -c "
               "\"import secrets; print(secrets.token_urlsafe(48))\"). Everyone is signed out once.")
STEP_PASSWORD = r"Change the admin password: cd backend, then .venv\Scripts\python.exe scripts\set_admin_password.py"
STEP_START = "Start Reconize again."


def fix_steps(result: dict) -> list[str]:
    """Only the steps for what was actually found."""
    if not result.get("any"):
        return []
    steps = [STEP_STOP]
    if result.get("jwt_secret_default"):
        steps.append(STEP_SECRET)
    if result.get("admin_password_default"):
        steps.append(STEP_PASSWORD)
    return steps + [STEP_START]


def check(session: Session, jwt_secret: str | None = None) -> dict:
    """{"jwt_secret_default", "admin_password_default", "any"} - booleans only."""
    from app.auth.security import verify_password
    from app.config import JWT_SECRET
    from app.models.models import User

    secret = JWT_SECRET if jwt_secret is None else jwt_secret
    jwt_default = (not secret) or secret == DEFAULT_JWT_SECRET
    admin_default = False
    for user in session.exec(select(User).where(User.role == "admin")).all():
        try:
            if verify_password(DEFAULT_ADMIN_PASSWORD, user.password_hash):
                admin_default = True
                break
        except ValueError:  # a malformed hash is not the default password
            continue
    return {"jwt_secret_default": jwt_default, "admin_password_default": admin_default,
            "any": jwt_default or admin_default}


def warn_at_startup(session: Session) -> dict:
    result = check(session)
    if result["jwt_secret_default"]:
        logger.warning("SECURITY: JWT_SECRET is the public default - anyone can forge sign-in tokens. "
                       "Set a random JWT_SECRET in .env (README: First Sign-in and Passwords).")
    if result["admin_password_default"]:
        logger.warning("SECURITY: an admin account still uses the public default password. "
                       "Change it: cd backend && .venv\\Scripts\\python.exe scripts\\set_admin_password.py")
    return result
