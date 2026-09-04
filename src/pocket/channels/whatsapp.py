"""WhatsApp via Twilio (sandbox for now, §6.1).

Inbound: Twilio POSTs form-encoded params, signed with X-Twilio-Signature
(HMAC-SHA1 over the full URL + sorted params, keyed with the auth token).
Outbound: Twilio Messages REST API.

Quick replies: real WhatsApp buttons need approved Content templates, which the
sandbox doesn't give you, so options are rendered as a numbered/lettered hint in
the text. The user replies with the value, which is exactly what the
orchestrator expects anyway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from pocket.channels.base import InboundMessage, Location, MediaAttachment, OutboundMessage

log = logging.getLogger(__name__)

MAX_LEN = 1600  # twilio's whatsapp body limit


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio(
    auth_token: str, url: str, params: Mapping[str, str], signature: str | None
) -> bool:
    if not signature:
        return False
    candidates = {url}
    # behind a proxy the scheme/port can differ from what twilio signed
    if url.startswith("http://"):
        candidates.add("https://" + url[len("http://") :])
    return any(
        hmac.compare_digest(twilio_signature(auth_token, u, params), signature) for u in candidates
    )


def parse_twilio(form: Mapping[str, str], auth: tuple[str, str] | None) -> InboundMessage | None:
    sid = form.get("MessageSid") or form.get("SmsMessageSid")
    sender = form.get("From")
    if not sid or not sender:
        return None
    media = []
    for i in range(int(form.get("NumMedia", "0") or 0)):
        url = form.get(f"MediaUrl{i}")
        if url:
            media.append(
                MediaAttachment(url=url, content_type=form.get(f"MediaContentType{i}"), auth=auth)
            )
    location = None
    if form.get("Latitude") and form.get("Longitude"):
        location = Location(
            lat=float(form["Latitude"]),
            lon=float(form["Longitude"]),
            label=form.get("Address") or form.get("Label"),
        )
    # tapped quick-reply buttons (if content templates are ever set up)
    text = form.get("ButtonPayload") or form.get("Body") or None
    return InboundMessage(
        channel="whatsapp",
        channel_msg_id=sid,
        user_channel_id=sender,
        text=text,
        media=media,
        location=location,
        received_at=datetime.now(UTC),
    )


def render_text(msg: OutboundMessage) -> str:
    """WhatsApp supports *bold*; options become a compact hint line."""
    text = msg.text
    if msg.options and msg.options_style == "buttons":
        values = [o.value for o in msg.options]
        # the text usually already explains a/b/c or numbers; add a hint only for yes/no
        if values[:2] == ["yes", "no"] and "yes" not in text.lower():
            text += "\n(yes / no)"
    return text


def chunks(text: str, size: int = MAX_LEN) -> list[str]:
    if len(text) <= size:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


class WhatsAppAdapter:
    name = "whatsapp"

    def __init__(
        self,
        account_sid: str | None,
        auth_token: str | None,
        from_number: str | None,
        client: httpx.AsyncClient | None = None,
    ):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.from_number = from_number
        self.client = client or httpx.AsyncClient(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    @property
    def media_auth(self) -> tuple[str, str] | None:
        return (self.account_sid, self.auth_token) if self.account_sid and self.auth_token else None

    async def send(
        self, user_channel_id: str, message: OutboundMessage, meta: dict[str, Any] | None = None
    ) -> None:
        if not self.configured:
            log.warning("whatsapp not configured, dropping reply: %s", message.text[:80])
            return
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        for part in chunks(render_text(message)):
            data = {"From": self.from_number, "To": user_channel_id, "Body": part}
            if message.attachment_url:
                data["MediaUrl"] = message.attachment_url
            r = await self.client.post(url, data=data, auth=(self.account_sid, self.auth_token))  # type: ignore[arg-type]
            if r.status_code >= 400:
                raise RuntimeError(f"twilio {r.status_code}: {r.text[:300]}")
