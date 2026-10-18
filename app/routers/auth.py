"""Session auth for mutating and admin-visibility routes. Fail-closed.

Rule: missing, stale, expired, unknown or conflicting evidence never acts.
While ADMIN_USERNAME or ADMIN_PASSWORD is unset, login issues no sessions and
every protected route returns 401. Reads and citizen intake stay public.

Sessions are in-memory per process: a restart logs admins out (acceptable for
the demo; persistent sessions are a post-freeze item).

Env (compose passthrough): ADMIN_USERNAME, ADMIN_PASSWORD.
"""
import os
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter()

COOKIE_NAME = "reporta_session"
SESSION_TTL_SECONDS = 8 * 3600

# token -> expiry (unix seconds). Access is lock-guarded; the store is small
# (one demo admin), so no eviction beyond lazy expiry checks.
_sessions: dict[str, float] = {}
_lock = threading.Lock()


def admin_configured() -> bool:
    """True only when BOTH admin credentials are set and non-empty."""
    return bool(os.environ.get("ADMIN_USERNAME")) and bool(
        os.environ.get("ADMIN_PASSWORD")
    )


def require_admin(request: Request) -> None:
    """FastAPI dependency: 401 unless a live admin session cookie is present
    AND admin credentials are configured (fail-closed on unset creds)."""
    if not admin_configured():
        raise HTTPException(401, "admin auth not configured")
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "admin session required")
    now = time.time()
    with _lock:
        expiry = _sessions.get(token)
        if expiry is None:
            raise HTTPException(401, "admin session required")
        if expiry < now:
            _sessions.pop(token, None)
            raise HTTPException(401, "admin session expired")
    return None


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/admin/login")
def login(payload: LoginIn, response: Response):
    if not admin_configured():
        # Fail closed: with no configured admin there is nothing to
        # authenticate against, so no session is ever issued.
        raise HTTPException(503, "admin auth not configured")
    user_ok = secrets.compare_digest(
        payload.username, os.environ["ADMIN_USERNAME"]
    )
    pass_ok = secrets.compare_digest(
        payload.password, os.environ["ADMIN_PASSWORD"]
    )
    if not (user_ok and pass_ok):
        raise HTTPException(401, "invalid credentials")
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _sessions[token] = now + SESSION_TTL_SECONDS
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
    )
    return {"ok": True}


@router.post("/admin/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with _lock:
            _sessions.pop(token, None)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
