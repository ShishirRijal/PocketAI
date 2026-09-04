"""CLI / HTTP test channel.

Replies are collected in an in-memory outbox keyed by user id, so
POST /webhook/cli can wait for them and return them in the response. That makes
curl and the `pocket chat --remote` REPL work against a running server.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from pocket.channels.base import OutboundMessage


class CliAdapter:
    name = "cli"

    def __init__(self) -> None:
        self.outbox: dict[str, list[OutboundMessage]] = defaultdict(list)
        self._events: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def send(
        self, user_channel_id: str, message: OutboundMessage, meta: dict[str, Any] | None = None
    ) -> None:
        self.outbox[user_channel_id].append(message)
        self._events[user_channel_id].set()

    def drain(self, user_channel_id: str) -> list[OutboundMessage]:
        msgs = self.outbox.pop(user_channel_id, [])
        self._events[user_channel_id].clear()
        return msgs


def render_terminal(msg: OutboundMessage) -> str:
    text = msg.text
    if msg.options:
        text += "\n  " + "  ".join(f"[{o.value}] {o.label}" for o in msg.options)
    return text
