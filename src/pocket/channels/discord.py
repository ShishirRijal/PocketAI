"""Discord via the Interactions endpoint (HTTP, no gateway websocket).

- Slash command `/pocket text:<message>` and button clicks arrive as signed POSTs
  (Ed25519 over timestamp + body, X-Signature-Ed25519 / X-Signature-Timestamp).
- We answer with a deferred response (type 5) immediately, then post the real
  reply as a follow-up on the interaction webhook once the worker is done.
- Proactive messages (digest) go out as a DM with the bot token.

Plain DMs without a slash command would need a gateway connection; left out on
purpose to keep this a stateless HTTP service.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pocket.channels.base import InboundMessage, OutboundMessage

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"

PING, APP_COMMAND, COMPONENT = 1, 2, 3
PONG, DEFERRED, DEFERRED_UPDATE = 1, 5, 6


def verify_discord(
    public_key_hex: str | None, signature: str | None, timestamp: str | None, body: bytes
) -> bool:
    if not (public_key_hex and signature and timestamp):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


def _user_id(interaction: dict[str, Any]) -> str | None:
    user = interaction.get("user") or (interaction.get("member") or {}).get("user") or {}
    return user.get("id")


def parse_interaction(interaction: dict[str, Any]) -> InboundMessage | None:
    kind = interaction.get("type")
    uid = _user_id(interaction)
    if not uid:
        return None
    meta = {
        "interaction_token": interaction["token"],
        "application_id": interaction["application_id"],
    }
    if kind == APP_COMMAND:
        opts = {
            o["name"]: o.get("value") for o in (interaction.get("data") or {}).get("options", [])
        }
        text = opts.get("text") or opts.get("message") or ""
        media = []
        if att_id := opts.get("receipt"):
            att = (interaction["data"].get("resolved") or {}).get("attachments", {}).get(att_id)
            if att:
                from pocket.channels.base import MediaAttachment

                media.append(MediaAttachment(url=att["url"], content_type=att.get("content_type")))
        return InboundMessage(
            channel="discord",
            channel_msg_id=interaction["id"],
            user_channel_id=uid,
            text=str(text),
            media=media,
            received_at=datetime.now(UTC),
            meta=meta,
        )
    if kind == COMPONENT:
        return InboundMessage(
            channel="discord",
            channel_msg_id=interaction["id"],
            user_channel_id=uid,
            text=(interaction.get("data") or {}).get("custom_id", ""),
            received_at=datetime.now(UTC),
            meta=meta,
        )
    return None


def components(msg: OutboundMessage) -> list[dict[str, Any]]:
    if not msg.options:
        return []
    if msg.options_style == "list" and len(msg.options) > 5:
        return [
            {
                "type": 1,
                "components": [
                    {
                        "type": 3,
                        "custom_id": "pick",
                        "options": [
                            {"label": o.label[:100], "value": o.value[:100]}
                            for o in msg.options[:25]
                        ],
                    }
                ],
            }
        ]
    buttons = [
        {"type": 2, "style": 1 if i == 0 else 2, "label": o.label[:80], "custom_id": o.value[:100]}
        for i, o in enumerate(msg.options[:5])
    ]
    return [{"type": 1, "components": buttons}]


COMMANDS = [
    {
        "name": "pocket",
        "description": "Talk to Pocket: log, fix or ask about spending",
        "options": [
            {
                "type": 3,
                "name": "text",
                "description": "e.g. 23 eur groceries at rimi",
                "required": True,
            }
        ],
    },
    {
        "name": "receipt",
        "description": "Log a receipt photo",
        "options": [
            {
                "type": 11,
                "name": "receipt",
                "description": "photo of the receipt",
                "required": True,
            },
            {"type": 3, "name": "text", "description": "optional note", "required": False},
        ],
    },
]


class DiscordAdapter:
    name = "discord"

    def __init__(
        self,
        bot_token: str | None,
        application_id: str | None,
        client: httpx.AsyncClient | None = None,
    ):
        self.bot_token = bot_token
        self.application_id = application_id
        self.client = client or httpx.AsyncClient(timeout=10)

    async def send(
        self, user_channel_id: str, message: OutboundMessage, meta: dict[str, Any] | None = None
    ) -> None:
        body: dict[str, Any] = {"content": message.text[:2000], "components": components(message)}
        if meta and meta.get("channel_id") and self.bot_token:
            # a plain message read by the gateway bot: answer in the same channel
            if meta.get("message_id"):
                body["message_reference"] = {
                    "message_id": meta["message_id"],
                    "fail_if_not_exists": False,
                }
            r = await self.client.post(
                f"{API}/channels/{meta['channel_id']}/messages",
                json=body,
                headers={"Authorization": f"Bot {self.bot_token}"},
            )
        elif meta and meta.get("interaction_token"):
            url = f"{API}/webhooks/{meta['application_id']}/{meta['interaction_token']}"
            r = await self.client.post(url, json=body)
        elif self.bot_token:
            headers = {"Authorization": f"Bot {self.bot_token}"}
            dm = await self.client.post(
                f"{API}/users/@me/channels", json={"recipient_id": user_channel_id}, headers=headers
            )
            dm.raise_for_status()
            r = await self.client.post(
                f"{API}/channels/{dm.json()['id']}/messages", json=body, headers=headers
            )
        else:
            log.warning("discord not configured for proactive messages")
            return
        if r.status_code >= 400:
            raise RuntimeError(f"discord {r.status_code}: {r.text[:300]}")

    async def register_commands(self) -> Any:
        r = await self.client.put(
            f"{API}/applications/{self.application_id}/commands",
            json=COMMANDS,
            headers={"Authorization": f"Bot {self.bot_token}"},
        )
        return r.json()
