from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth.deps import bearer_scheme, get_api_client, get_station_user, is_machine
from app.auth.security import (
    FILE_COOKIE,
    create_access_token,
    create_file_token,
    decode_access_token,
    verify_password,
)
from app.database.db import get_session
from app.models.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str


def _set_file_cookie(request: Request, response: Response, user: User, access_token: str) -> None:
    """HttpOnly, SameSite=Strict, sent only to /api/files, expiring with the
    session token it is derived from. Lets <img>/<a> previews of stored files
    authenticate without putting any token in a URL."""
    payload = decode_access_token(access_token) or {}
    expires = datetime.fromtimestamp(payload.get("exp", 0), tz=timezone.utc)
    seconds = int((expires - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return
    response.set_cookie(FILE_COOKIE, create_file_token(user.id, expires), max_age=seconds, path="/api/files",
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == body.username)).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password.")
    token = create_access_token(user.id, user.username)
    if not is_machine(user):  # machines never read stored files
        _set_file_cookie(request, response, user, token)
    return LoginResponse(access_token=token, username=user.username)


@router.post("/file-session", status_code=204)
def start_file_session(request: Request, response: Response,
                       credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                       user: User = Depends(get_station_user)):
    """(Re)issue the file-read cookie for an already signed-in browser tab -
    e.g. a session that began before this cookie existed. Any human account
    (stations included) may call it; machine accounts may not."""
    _set_file_cookie(request, response, user, credentials.credentials)


@router.delete("/file-session", status_code=204)
def end_file_session(response: Response):
    """Sign-out: drop the file-read cookie. Needs no token (it may already
    have expired), and only ever removes this browser's own cookie."""
    response.delete_cookie(FILE_COOKIE, path="/api/files")


@router.get("/me")
def me(user: User = Depends(get_api_client)):
    return {"id": user.id, "username": user.username, "role": user.role}
