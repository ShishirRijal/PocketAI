"""Edge -> core handoff: allowlist, rate limit, persist raw (idempotent), enqueue.

Persist first, process second: once this returns, the message can't be lost,
even if every LLM provider is down or the worker crashes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from pocket.channels.base import InboundMessage
from pocket.config import Settings
from pocket.core.queue import MessageQueue
from pocket.core.ratelimit import RateLimiter
from pocket.data.db import Database
from pocket.data.repositories import RawMessageRepo, UserRepo

log = logging.getLogger(__name__)


class IngestStatus(StrEnum):
    QUEUED = "queued"
    DUPLICATE = "duplicate"  # provider retried the webhook
    UNKNOWN_USER = "unknown_user"  # not on the allowlist: silence
    RATE_LIMITED = "rate_limited"


@dataclass
class IngestResult:
    status: IngestStatus
    raw_message_id: int | None = None
    user_id: int | None = None


class Ingestor:
    def __init__(self, db: Database, queue: MessageQueue, limiter: RateLimiter, settings: Settings):
        self.db = db
        self.queue = queue
        self.limiter = limiter
        self.settings = settings

    async def ingest(self, msg: InboundMessage, *, enqueue: bool = True) -> IngestResult:
        with self.db.session() as s:
            user = UserRepo(s).by_identity(msg.channel, msg.user_channel_id)
            if user is None:
                log.warning(
                    "dropping message from unknown %s user %s", msg.channel, msg.user_channel_id
                )
                return IngestResult(IngestStatus.UNKNOWN_USER)
            media: list[dict] = [m.model_dump() for m in msg.media]
            if msg.location:
                media.append({"type": "location", **msg.location.model_dump()})
            raw, created = RawMessageRepo(s).insert_idempotent(
                user_id=user.id,
                channel=msg.channel,
                channel_msg_id=msg.channel_msg_id,
                text=msg.text,
                media_json=media or None,
                received_at=msg.received_at,
                user_channel_id=msg.user_channel_id,
                channel_meta=msg.meta or None,
            )
            if not created:
                return IngestResult(IngestStatus.DUPLICATE, raw.id, user.id)
            allowed = self.limiter.hit(f"msg:{user.id}", self.settings.rate_limit_per_min, 60)
            if not allowed:
                RawMessageRepo(s).mark(raw, "rate_limited")
                return IngestResult(IngestStatus.RATE_LIMITED, raw.id, user.id)
            raw_id, user_id = raw.id, user.id
        if enqueue:
            await self.queue.enqueue(raw_id)
        return IngestResult(IngestStatus.QUEUED, raw_id, user_id)
