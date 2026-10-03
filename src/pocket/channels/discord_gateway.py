"""Discord bot mode: plain messages, no slash command needed.

The interactions endpoint (channels/discord.py) only receives slash commands and
button clicks. To answer ordinary messages ("23 eur lunch") the bot has to hold
a gateway websocket open, which this does via discord.py, inside the same
process as the API.

What it listens to:
- DMs to the bot (always)
- messages that @mention the bot, anywhere it can see
- every message in POCKET_DISCORD_CHANNEL_ID, if set (needs "Message Content
  Intent" switched on in the developer portal: Bot → Privileged Gateway Intents)

Messages go through the normal ingest path (allowlist, idempotency, raw
message stored) and replies are posted back in the same channel. Buttons in
replies still arrive through the interactions endpoint, as before.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

import discord

from pocket.channels.base import InboundMessage, MediaAttachment

if TYPE_CHECKING:
    from pocket.runtime import Runtime

log = logging.getLogger(__name__)


def to_inbound(message: Any, bot_user_id: int | None) -> InboundMessage:
    text = message.content or ""
    if bot_user_id is not None:
        # "@Pocket 23 eur lunch" -> "23 eur lunch"
        text = re.sub(rf"<@!?{bot_user_id}>", "", text)
    media = [
        MediaAttachment(url=a.url, content_type=a.content_type)
        for a in getattr(message, "attachments", [])
        if (a.content_type or "").startswith(("image/", "audio/"))
    ]
    return InboundMessage(
        channel="discord",
        channel_msg_id=f"msg:{message.id}",
        user_channel_id=str(message.author.id),
        text=text.strip() or None,
        media=media,
        meta={"channel_id": str(message.channel.id), "message_id": str(message.id)},
    )


def should_handle(message: Any, bot_user: Any, watch_channel_id: str | None) -> bool:
    if message.author.bot or (bot_user is not None and message.author.id == bot_user.id):
        return False
    if isinstance(message.channel, discord.DMChannel) or getattr(message.guild, "id", None) is None:
        return True
    if watch_channel_id and str(message.channel.id) == str(watch_channel_id):
        return True
    return bot_user is not None and any(u.id == bot_user.id for u in message.mentions)


class GatewayBot:
    def __init__(self, runtime: Runtime, token: str, watch_channel_id: str | None = None):
        self.runtime = runtime
        self.token = token
        self.watch_channel_id = watch_channel_id
        self.client: discord.Client | None = None
        self._task: asyncio.Task | None = None

    def _make_client(self, message_content: bool) -> discord.Client:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = message_content
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            log.info(
                "discord bot connected as %s (message content intent: %s)",
                client.user,
                message_content,
            )

        @client.event
        async def on_message(message: discord.Message) -> None:
            if not should_handle(message, client.user, self.watch_channel_id):
                return
            await self.handle(message, client.user.id if client.user else None)

        return client

    async def handle(self, message: Any, bot_user_id: int | None) -> None:
        msg = to_inbound(message, bot_user_id)
        if not msg.text and not msg.media:
            return
        res = await self.runtime.ingestor.ingest(msg, enqueue=False)
        if res.raw_message_id is None or res.status.value != "queued":
            if res.status.value == "unknown_user":
                log.info(
                    "ignoring discord message from %s (not on the allowlist)", msg.user_channel_id
                )
            return
        try:
            async with message.channel.typing():
                await self.runtime.dispatcher.process(res.raw_message_id)
        except Exception:
            log.exception("discord message %s failed", message.id)

    async def _run(self) -> None:
        # try with the privileged Message Content intent first (needed for a watched
        # channel); if the portal hasn't enabled it, fall back to DMs + mentions
        for message_content in (True, False):
            self.client = self._make_client(message_content)
            try:
                await self.client.start(self.token)
                return
            except discord.errors.PrivilegedIntentsRequired:
                log.warning(
                    "Message Content Intent is off in the Discord portal; "
                    "falling back to DMs and @mentions only"
                )
                await self.client.close()
            except discord.errors.LoginFailure:
                log.error("discord bot token rejected; bot mode disabled")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discord gateway crashed")
                return

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self._task is not None:
            self._task.cancel()
