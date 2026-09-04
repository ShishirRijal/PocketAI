"""POST /webhook/{channel}: verify -> normalize -> persist -> enqueue -> ack fast.

Twilio wants a 200 within ~5 s and retries on anything else, so nothing slow
happens here. Retries are harmless: raw_messages is unique on
(channel, channel_msg_id).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pocket.channels.base import InboundMessage
from pocket.channels.cli import render_terminal
from pocket.channels.discord import (
    APP_COMMAND,
    COMPONENT,
    DEFERRED,
    PING,
    PONG,
    parse_interaction,
    verify_discord,
)
from pocket.channels.telegram import parse_update, verify_telegram
from pocket.channels.whatsapp import parse_twilio, verify_twilio
from pocket.core.ingest import IngestStatus
from pocket.runtime import Runtime

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhooks"])

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def rt(request: Request) -> Runtime:
    return request.app.state.runtime


def _public_url(request: Request) -> str:
    """The URL twilio signed. Prefer the configured public URL over what the
    proxy forwarded to us."""
    settings = rt(request).settings
    base = settings.public_url.rstrip("/")
    path = request.url.path
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{path}{query}"


@router.post("/whatsapp")
async def whatsapp(
    request: Request, x_twilio_signature: str | None = Header(default=None)
) -> Response:
    runtime = rt(request)
    s = runtime.settings
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if s.verify_signatures and (
        not s.twilio_auth_token
        or not verify_twilio(s.twilio_auth_token, _public_url(request), params, x_twilio_signature)
    ):
        log.warning("bad twilio signature")
        raise HTTPException(403, "bad signature")
    msg = parse_twilio(params, runtime.adapters["whatsapp"].media_auth)  # type: ignore[attr-defined]
    if msg:
        res = await runtime.ingestor.ingest(msg)
        if res.status is IngestStatus.RATE_LIMITED:
            from pocket.core.render import RATE_LIMITED

            return Response(
                f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{RATE_LIMITED}</Message></Response>',
                media_type="application/xml",
            )
    return Response(EMPTY_TWIML, media_type="application/xml")


@router.post("/telegram/{secret}")
@router.post("/telegram")
async def telegram(
    request: Request,
    secret: str | None = None,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    runtime = rt(request)
    s = runtime.settings
    if s.verify_signatures and not verify_telegram(
        s.telegram_webhook_secret, secret, x_telegram_bot_api_secret_token
    ):
        raise HTTPException(403, "bad secret")
    update = await request.json()
    msg = parse_update(update)
    if msg:
        await runtime.ingestor.ingest(msg)
    return {"ok": True}


@router.post("/discord")
async def discord(
    request: Request,
    x_signature_ed25519: str | None = Header(default=None),
    x_signature_timestamp: str | None = Header(default=None),
) -> JSONResponse:
    runtime = rt(request)
    s = runtime.settings
    body = await request.body()
    if s.verify_signatures and not verify_discord(
        s.discord_public_key, x_signature_ed25519, x_signature_timestamp, body
    ):
        raise HTTPException(401, "invalid request signature")
    interaction = json.loads(body)
    kind = interaction.get("type")
    if kind == PING:
        return JSONResponse({"type": PONG})
    if kind in (APP_COMMAND, COMPONENT):
        msg = parse_interaction(interaction)
        if msg:
            res = await runtime.ingestor.ingest(msg)
            if res.status is IngestStatus.UNKNOWN_USER:
                return JSONResponse(
                    {"type": 4, "data": {"content": "🔒 This is a private bot.", "flags": 64}}
                )
        # "thinking…" now, real answer arrives as a follow-up from the worker
        return JSONResponse({"type": DEFERRED})
    return JSONResponse({"type": PONG})


class CliIn(BaseModel):
    text: str
    user: str | None = None  # defaults to the configured owner cli identity
    msg_id: str | None = None


@router.post("/cli")
async def cli(
    request: Request, body: CliIn, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Synchronous test channel: persists like any channel, then processes inline
    and returns the replies in the response."""
    runtime = rt(request)
    s = runtime.settings
    if (s.env == "prod" or s.admin_token) and authorization != f"Bearer {s.admin_token}":
        raise HTTPException(401, "admin token required")
    import uuid

    ucid = body.user or s.owner_cli
    msg = InboundMessage(
        channel="cli",
        channel_msg_id=body.msg_id or uuid.uuid4().hex,
        user_channel_id=ucid,
        text=body.text,
    )
    res = await runtime.ingestor.ingest(msg, enqueue=False)
    if res.status is IngestStatus.UNKNOWN_USER:
        raise HTTPException(403, "unknown cli user")
    if res.status is not IngestStatus.QUEUED or res.raw_message_id is None:
        return {"status": res.status.value, "replies": []}
    await runtime.dispatcher.process(res.raw_message_id)
    replies = runtime.cli.drain(ucid)
    return {
        "status": "ok",
        "raw_message_id": res.raw_message_id,
        "replies": [r.model_dump() for r in replies],
        "text": "\n\n".join(render_terminal(r) for r in replies),
    }
