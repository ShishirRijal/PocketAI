"""Worker side: take a raw message id, run the orchestrator, send the replies
back out through the channel it came from."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pocket.channels.base import ChannelAdapter, OutboundMessage
from pocket.core.orchestrator import Orchestrator
from pocket.data.db import Database
from pocket.data.repositories import RawMessageRepo, UserRepo

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self, db: Database, orchestrator: Orchestrator, adapters: dict[str, ChannelAdapter]
    ):
        self.db = db
        self.orchestrator = orchestrator
        self.adapters = adapters

    async def process(self, raw_message_id: int) -> list[OutboundMessage]:
        replies = await self.orchestrator.handle_raw(raw_message_id)
        if not replies:
            return replies
        with self.db.session() as s:
            raw = RawMessageRepo(s).get(raw_message_id)
            if raw is None:
                return replies
            channel, ucid, meta = raw.channel, raw.user_channel_id, raw.channel_meta
            if ucid is None:
                ucid = UserRepo(s).identity_for(raw.user_id, channel)
        adapter = self.adapters.get(channel)
        if adapter is None or ucid is None:
            log.warning("no adapter/recipient for %s message %s", channel, raw_message_id)
            return replies
        for r in replies:
            await send_with_retry(adapter, ucid, r, meta)
        return replies

    async def send_to_user(
        self, user_id: int, message: OutboundMessage, channel: str | None = None
    ) -> bool:
        """Proactive messages (digest, reminders). Tries the user's primary channel
        first, then any other linked one. WhatsApp only allows free-form messages
        within 24h of the user's last message, so a Sunday digest can bounce there
        and still arrive on Telegram."""
        with self.db.session() as s:
            user = UserRepo(s).get(user_id)
            if user is None:
                return False
            order = [channel, user.primary_channel, "whatsapp", "telegram", "discord", "cli", "web"]
            targets: list[tuple[str, str]] = []
            for ch in dict.fromkeys(c for c in order if c):
                ucid = UserRepo(s).identity_for(user_id, ch)
                if ucid and ch in self.adapters:
                    targets.append((ch, ucid))
        if not targets:
            log.warning("user %s has no reachable channel", user_id)
            return False
        for ch, ucid in targets:
            if await send_with_retry(self.adapters[ch], ucid, message, None, attempts=2):
                return True
            log.warning(
                "proactive message to user %s failed on %s, trying next channel", user_id, ch
            )
        return False


async def send_with_retry(
    adapter: ChannelAdapter,
    ucid: str,
    message: OutboundMessage,
    meta: dict[str, Any] | None,
    attempts: int = 3,
) -> bool:
    for i in range(attempts):
        try:
            await adapter.send(ucid, message, meta)
            return True
        except Exception as e:
            log.warning("send via %s failed (try %d/%d): %s", adapter.name, i + 1, attempts, e)
            await asyncio.sleep(0.5 * 2**i)
    log.error("giving up sending to %s via %s", ucid, adapter.name)
    return False
