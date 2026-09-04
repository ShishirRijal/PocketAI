"""The web dashboard at /app (§12.14): overview charts, filterable transactions,
edit/delete with full history, and a system view of LLM cost and health."""

from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from pocket.web import api
from pocket.web.auth import COOKIE, auth_required, check_password, current_user_id, make_cookie

STATIC = Path(__file__).parent / "static"

router = APIRouter(tags=["dashboard"])
router.include_router(api.router)

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pocket · sign in</title>
<link rel="stylesheet" href="/app/static/app.css"></head>
<body class="login-body"><form class="login card" method="post" action="/app/login">
<div class="brand"><span class="brand-mark">P</span> Pocket</div>
<p class="muted">Your money log. Sign in with your admin token.</p>
{error}
<label class="field"><span>Token</span><input type="password" name="token" autocomplete="current-password" autofocus required></label>
<button class="btn primary" type="submit">Sign in</button></form></body></html>"""


@router.get("/app", response_class=HTMLResponse, include_in_schema=False)
async def app_page(request: Request, token: str | None = None) -> Response:
    settings = request.app.state.runtime.settings
    if token and check_password(settings, token):
        # one-click links from the admin pages; swap the token for a cookie
        rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
        resp = RedirectResponse(f"/app?{urlencode(rest)}" if rest else "/app", status_code=303)
        _set_cookie(resp, request)
        return resp
    if current_user_id(request) is None:
        return RedirectResponse("/app/login", status_code=303)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/app/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    if not auth_required(request.app.state.runtime.settings):
        return HTMLResponse(status_code=303, headers={"Location": "/app"})
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(LOGIN.format(error=err))


@router.post("/app/login", include_in_schema=False)
async def login(request: Request, token: str = Form(...)) -> Response:
    settings = request.app.state.runtime.settings
    if not check_password(settings, token):
        return RedirectResponse("/app/login?error=Wrong+token", status_code=303)
    resp = RedirectResponse("/app", status_code=303)
    _set_cookie(resp, request)
    return resp


@router.get("/app/logout", include_in_schema=False)
async def logout() -> Response:
    resp = RedirectResponse("/app/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/app/static/{name}", include_in_schema=False)
async def static(name: str) -> Response:
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})


def _set_cookie(resp: Response, request: Request) -> None:
    rt = request.app.state.runtime
    settings = rt.settings
    resp.set_cookie(
        COOKIE,
        make_cookie(settings, rt.owner_id),
        max_age=settings.dashboard_session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.public_url.startswith("https"),
        path="/",
    )
