"""Message queue between the webhook and the worker.

- InlineQueue: an asyncio.Queue consumed inside the api process. Enough for one
  user (doc §15.3), and zero ops.
- RedisStreamQueue: Redis Streams + consumer group, for running N workers in
  separate containers. Unacked entries are reclaimed after a timeout, so a
  worker dying mid-message doesn't lose it.

Both hand the worker a raw_message_id; the message body is always read from the
DB, which is the source of truth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

Handler = Callable[[int], Awaitable[Any]]


class MessageQueue(Protocol):
    async def enqueue(self, raw_message_id: int) -> None: ...

    async def run(self, handler: Handler, stop: asyncio.Event) -> None: ...


class InlineQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue[int] = asyncio.Queue()

    async def enqueue(self, raw_message_id: int) -> None:
        await self._q.put(raw_message_id)

    def qsize(self) -> int:
        return self._q.qsize()

    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                raw_id = await asyncio.wait_for(self._q.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await handler(raw_id)
            except Exception:
                log.exception("handler failed for raw message %s", raw_id)
            finally:
                self._q.task_done()

    async def join(self) -> None:
        await self._q.join()


class RedisStreamQueue:
    STREAM = "message.inbound"
    GROUP = "workers"

    def __init__(self, redis, *, consumer: str | None = None, claim_idle_ms: int = 60_000):
        self.redis = redis  # redis.asyncio.Redis, decode_responses=True
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.claim_idle_ms = claim_idle_ms

    async def _ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, raw_message_id: int) -> None:
        await self.redis.xadd(self.STREAM, {"raw_message_id": str(raw_message_id)}, maxlen=10_000)

    async def run(self, handler: Handler, stop: asyncio.Event) -> None:
        await self._ensure_group()
        while not stop.is_set():
            # first pick up anything a dead worker left behind
            entries = await self._claim_stale()
            if not entries:
                resp = await self.redis.xreadgroup(
                    self.GROUP, self.consumer, {self.STREAM: ">"}, count=10, block=1000
                )
                entries = [e for _, items in (resp or []) for e in items]
            for entry_id, fields in entries:
                try:
                    await handler(int(fields["raw_message_id"]))
                except Exception:
                    log.exception("handler failed for stream entry %s", entry_id)
                    continue  # not acked -> reclaimed later
                await self.redis.xack(self.STREAM, self.GROUP, entry_id)

    async def _claim_stale(self) -> list:
        try:
            res = await self.redis.xautoclaim(
                self.STREAM, self.GROUP, self.consumer, min_idle_time=self.claim_idle_ms, count=10
            )
        except Exception:
            return []
        return [(eid, f) for eid, f in (res[1] if res else []) if f]
