"""Dashboard auth. Single user, so: the admin token is the password, and a
signed, expiring cookie keeps you logged in. Bearer tokens work too, for scripts
(Raycast, Obsidian, curl)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import HTTPException, Request
from sqlalchemy import select

from pocket.config import Settings
from pocket.data.models import User

COOKIE = "pocket_session"


def _key(settings: Settings) -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()
    return hashlib.sha256(f"pocket-cookie:{settings.admin_token or 'dev'}".encode()).digest()


def make_cookie(settings: Settings, user_id: int, now: float | None = None) -> str:
    exp = int((now or time.time()) + settings.dashboard_session_days * 86400)
    payload = f"{user_id}.{exp}"
    sig = hmac.new(_key(settings), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def read_cookie(settings: Settings, value: str | None, now: float | None = None) -> int | None:
    if not value:
        return None
    try:
        uid, exp, sig = value.split(".")
        expected = hmac.new(_key(settings), f"{uid}.{exp}".encode(), hashlib.sha256).digest()
        given = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, given) or int(exp) < (now or time.time()):
        return None
    return int(uid)


def auth_required(settings: Settings) -> bool:
    return bool(settings.admin_token) or settings.env == "prod"


def check_password(settings: Settings, password: str) -> bool:
    return bool(settings.admin_token) and hmac.compare_digest(password, settings.admin_token)


def current_user_id(request: Request) -> int | None:
    runtime = request.app.state.runtime
    settings: Settings = runtime.settings
    if not auth_required(settings):
        return runtime.owner_id
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer ") and check_password(settings, authz[7:].strip()):
        return runtime.owner_id
    return read_cookie(settings, request.cookies.get(COOKIE))


def require_user(request: Request) -> User:
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(401, "login required")
    with request.app.state.runtime.services.db.session() as s:
        user = s.scalars(select(User).where(User.id == uid)).first()
        if user is None:
            raise HTTPException(401, "unknown user")
        s.expunge(user)
        return user
