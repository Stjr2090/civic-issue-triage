"""Stateless signed-JWT (HS256) admin auth. Fail-closed.

Rule: missing, stale, expired, unknown or conflicting evidence never acts.
While JWT_SECRET, ADMIN_USERNAME or ADMIN_PASSWORD is unset, login issues no
sessions and every protected route returns 401. Reads and citizen intake stay
public.

The session cookie carries a compact JWS:
base64url(header).base64url(payload).base64url(HMAC-SHA256). It is verified
statelessly on every request, so an unexpired token survives a server restart
(12h default lifetime).

Env (compose passthrough): ADMIN_USERNAME, ADMIN_PASSWORD, JWT_SECRET,
JWT_TTL_SECONDS, COOKIE_SECURE.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

router = APIRouter()

COOKIE_NAME = "reporta_session"
DEFAULT_SESSION_TTL_SECONDS = 12 * 3600
JWT_ALGORITHM = "HS256"


def admin_configured() -> bool:
    """True only when BOTH admin credentials are set and non-empty."""
    return bool(os.environ.get("ADMIN_USERNAME")) and bool(
        os.environ.get("ADMIN_PASSWORD")
    )


def jwt_secret() -> str:
    """Signing key, read at call time (never at import). Empty means unset."""
    return os.environ.get("JWT_SECRET", "")


def session_ttl_seconds() -> int:
    """Token lifetime: int(JWT_TTL_SECONDS) when set and positive, else 12h."""
    raw = os.environ.get("JWT_TTL_SECONDS")
    if raw:
        try:
            ttl = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_SESSION_TTL_SECONDS
        if ttl > 0:
            return ttl
    return DEFAULT_SESSION_TTL_SECONDS


def cookie_secure() -> bool:
    """Secure flag for the session cookie, env-gated (COOKIE_SECURE=1).

    The public face is HTTPS via the front proxy, so the box deploy sets
    COOKIE_SECURE=1. Default off keeps local http://localhost admin testing
    working (browsers silently drop Secure cookies over plain http).
    """
    return os.environ.get("COOKIE_SECURE", "").strip().lower() in (
        "1", "true", "yes"
    )


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.b64decode(segment + padding, altchars=b"-_", validate=True)


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(
        secret.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()


def issue_token(sub: str, now: float, ttl: int, secret: str) -> str:
    """Build a compact HS256 JWS with sub/iat/exp claims."""
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {"sub": sub, "iat": int(now), "exp": int(now) + ttl}
    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64url_encode(_sign(f"{h}.{p}".encode("ascii"), secret))
    return f"{h}.{p}.{signature}"


def _token_error(token: str, secret: str, now: float):
    """None when the token verifies, else the 401 detail to raise."""
    parts = token.split(".")
    if len(parts) != 3:
        return "admin session required"
    h_b64, p_b64, s_b64 = parts
    try:
        header = json.loads(_b64url_decode(h_b64))
        payload = json.loads(_b64url_decode(p_b64))
        provided = _b64url_decode(s_b64)
    except Exception:
        return "admin session required"
    if not isinstance(header, dict) or header.get("alg") != JWT_ALGORITHM:
        return "admin session required"
    expected = _sign(f"{h_b64}.{p_b64}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, provided):
        return "admin session required"
    if not isinstance(payload, dict):
        return "admin session required"
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return "admin session required"
    if exp <= now:
        return "admin session expired"
    return None


def require_admin(request: Request) -> None:
    """FastAPI dependency: 401 unless a stateless signed session cookie verifies
    AND admin credentials plus the signing key are configured (fail-closed)."""
    if not admin_configured() or not jwt_secret():
        raise HTTPException(401, "admin auth not configured")
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(401, "admin session required")
    error = _token_error(token, jwt_secret(), time.time())
    if error is not None:
        raise HTTPException(401, error)
    return None


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/admin/login")
def login(payload: LoginIn, response: Response):
    if not admin_configured() or not jwt_secret():
        # Fail closed: with no configured admin or signing key there is
        # nothing to authenticate against, so no session is ever issued.
        raise HTTPException(401, "admin auth not configured")
    user_ok = hmac.compare_digest(
        payload.username, os.environ["ADMIN_USERNAME"]
    )
    pass_ok = hmac.compare_digest(
        payload.password, os.environ["ADMIN_PASSWORD"]
    )
    if not (user_ok and pass_ok):
        raise HTTPException(401, "invalid credentials")
    ttl = session_ttl_seconds()
    token = issue_token(
        os.environ["ADMIN_USERNAME"], time.time(), ttl, jwt_secret()
    )
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        max_age=ttl,
    )
    return {"ok": True}


@router.post("/admin/logout")
def logout(request: Request, response: Response):
    # Stateless tokens cannot be revoked server-side; clearing the cookie is
    # the browser-facing contract. Remaining lifetime is the token's TTL.
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
