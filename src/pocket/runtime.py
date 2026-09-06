"""Long-running pieces: channel adapters, queue, worker loop, scheduler.

The api process and the worker process both build a Runtime; they differ only
in what they start (webhooks vs consumer loop), controlled by flags.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from pocket.channels.base import ChannelAdapter
from pocket.channels.cli import CliAdapter
from pocket.channels.discord import DiscordAdapter
from pocket.channels.telegram import TelegramAdapter
from pocket.channels.whatsapp import WhatsAppAdapter
from pocket.config import Settings
from pocket.core.dispatch import Dispatcher
from pocket.core.ingest import Ingestor
from pocket.core.queue import InlineQueue, MessageQueue, RedisStreamQueue
from pocket.wiring import Services, build_services, ensure_owner

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    services: Services
    adapters: dict[str, ChannelAdapter]
    queue: MessageQueue
    ingestor: Ingestor
    dispatcher: Dispatcher
    owner_id: int
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: list[asyncio.Task] = field(default_factory=list)
    scheduler: Any = None

    @property
    def cli(self) -> CliAdapter:
        return self.adapters["cli"]  # type: ignore[return-value]

    async def start(self, *, worker: bool, scheduler: bool) -> None:
        if worker:
            self.tasks.append(
                asyncio.create_task(self.queue.run(self.dispatcher.process, self.stop_event))
            )
            log.info("worker loop started (%s)", type(self.queue).__name__)
        if scheduler:
            from pocket.services.scheduler import build_scheduler

            self.scheduler = build_scheduler(self)
            self.scheduler.start()
            log.info("scheduler started")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        for t in self.tasks:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                t.cancel()
        await self.services.fx.aclose()


def build_adapters(settings: Settings) -> dict[str, ChannelAdapter]:
    return {
        "cli": CliAdapter(),
        "whatsapp": WhatsAppAdapter(
            settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_whatsapp_from
        ),
        "telegram": TelegramAdapter(settings.telegram_bot_token),
        "discord": DiscordAdapter(settings.discord_bot_token, settings.discord_application_id),
    }


def build_runtime(
    settings: Settings,
    *,
    services: Services | None = None,
    adapters: dict[str, ChannelAdapter] | None = None,
    **service_overrides: Any,
) -> Runtime:
    services = services or build_services(settings, **service_overrides)
    if settings.is_sqlite or settings.env != "prod":
        _migrate(services, settings)
    owner_id = ensure_owner(services.db, settings)
    adapters = adapters or build_adapters(settings)
    if settings.queue_backend == "redis" and services.redis is not None:
        queue: MessageQueue = RedisStreamQueue(services.redis)
    else:
        queue = InlineQueue()
    ingestor = Ingestor(services.db, queue, services.rate_limiter, settings)
    dispatcher = Dispatcher(services.db, services.orchestrator, adapters)
    services.extras["dispatcher"] = dispatcher
    services.extras["adapters"] = adapters
    return Runtime(
        settings=settings,
        services=services,
        adapters=adapters,
        queue=queue,
        ingestor=ingestor,
        dispatcher=dispatcher,
        owner_id=owner_id,
    )


def _migrate(services: Services, settings: Settings) -> None:
    """Run alembic to head. Tests with an in-memory schema skip this."""
    if settings.env == "test":
        services.db.create_all()
        return
    from pocket.data.migrate import upgrade

    upgrade(services.db)
