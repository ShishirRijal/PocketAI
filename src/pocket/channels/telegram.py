"""Telegram bot via webhook.

Auth: the webhook URL carries a secret path segment (/webhook/telegram/<secret>)
and we also set Telegram's secret_token so every update arrives with
X-Telegram-Bot-Api-Secret-Token. Either must match.

Options render as an inline keyboard; a tap comes back as a callback_query whose
data is the option value, which we feed in as if the user typed it.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from pocket.channels.base import InboundMessage, Location, MediaAttachment, OutboundMessage

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def verify_telegram(secret: str | None, path_secret: str | None, header_secret: str | None) -> bool:
    if not secret:
        return False
    return any(s and hmac.compare_digest(s, secret) for s in (path_secret, header_secret))


def parse_update(update: dict[str, Any]) -> InboundMessage | None:
    if cq := update.get("callback_query"):
        chat = (cq.get("message") or {}).get("chat") or {}
        return InboundMessage(
            channel="telegram",
            channel_msg_id=f"cq:{cq['id']}",
            user_channel_id=str(chat.get("id") or cq["from"]["id"]),
            text=cq.get("data"),
            meta={"callback_query_id": cq["id"]},
        )
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat_id = str(msg["chat"]["id"])
    media: list[MediaAttachment] = []
    if photos := msg.get("photo"):
        # largest size last; file_id is resolved to a URL when downloading
        media.append(
            MediaAttachment(url=f"tg-file:{photos[-1]['file_id']}", content_type="image/jpeg")
        )
    for key, ctype in (("voice", "audio/ogg"), ("audio", None), ("document", None)):
        if f := msg.get(key):
            media.append(
                MediaAttachment(
                    url=f"tg-file:{f['file_id']}", content_type=f.get("mime_type") or ctype
                )
            )
    location = None
    if loc := msg.get("location"):
        location = Location(lat=loc["latitude"], lon=loc["longitude"])
    prefix = "edit:" if "edited_message" in update else ""
    return InboundMessage(
        channel="telegram",
        channel_msg_id=f"{prefix}{chat_id}:{msg['message_id']}",
        user_channel_id=chat_id,
        text=msg.get("text") or msg.get("caption"),
        media=media,
        location=location,
        received_at=datetime.fromtimestamp(msg.get("date", 0), UTC)
        if msg.get("date")
        else datetime.now(UTC),
    )


def keyboard(msg: OutboundMessage) -> dict[str, Any] | None:
    if not msg.options:
        return None
    if msg.options_style == "list":
        rows = [[{"text": o.label, "callback_data": o.value[:64]}] for o in msg.options]
    else:
        rows = [[{"text": o.label, "callback_data": o.value[:64]} for o in msg.options]]
    return {"inline_keyboard": rows}


class TelegramAdapter:
    name = "telegram"

    def __init__(self, token: str | None, client: httpx.AsyncClient | None = None):
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    async def send(
        self, user_channel_id: str, message: OutboundMessage, meta: dict[str, Any] | None = None
    ) -> None:
        if not self.configured:
            log.warning("telegram not configured, dropping reply")
            return
        if meta and meta.get("callback_query_id"):
            # stop the button's loading spinner; failure here is harmless
            with contextlib.suppress(httpx.HTTPError):
                await self.client.post(
                    self._url("answerCallbackQuery"),
                    json={"callback_query_id": meta["callback_query_id"]},
                )
        body: dict[str, Any] = {"chat_id": user_channel_id, "text": message.text[:4096]}
        if kb := keyboard(message):
            body["reply_markup"] = kb
        if message.attachment_url:
            r = await self.client.post(
                self._url("sendDocument"),
                json={
                    "chat_id": user_channel_id,
                    "document": message.attachment_url,
                    "caption": message.text[:1024],
                },
            )
        else:
            r = await self.client.post(self._url("sendMessage"), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"telegram {r.status_code}: {r.text[:300]}")

    async def file_url(self, file_id: str) -> str:
        r = await self.client.get(self._url("getFile"), params={"file_id": file_id})
        r.raise_for_status()
        path = r.json()["result"]["file_path"]
        return f"{API}/file/bot{self.token}/{path}"

    async def set_webhook(self, public_url: str, secret: str) -> dict[str, Any]:
        r = await self.client.post(
            self._url("setWebhook"),
            json={
                "url": f"{public_url.rstrip('/')}/webhook/telegram/{secret}",
                "secret_token": secret,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            },
        )
        return r.json()
