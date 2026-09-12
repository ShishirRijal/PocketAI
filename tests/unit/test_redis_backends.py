import asyncio
from datetime import UTC, datetime

import fakeredis
import pytest

from pocket.core.queue import InlineQueue, RedisStreamQueue
from pocket.core.ratelimit import RateLimiter
from pocket.core.session import LastAction, RedisSessionStore, Session
from pocket.llm.quota import QuotaTracker


@pytest.fixture
def aredis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def test_redis_session_roundtrip(aredis):
    store = RedisSessionStore(aredis)
    s = Session(user_id=1)
    s.remember([10, 11])
    s.last_action = LastAction(kind="add", transaction_ids=[11], at=datetime.now(UTC))
    await store.save(s)
    got = await store.get(1)
    assert got.recent_transactions == [11, 10]
    assert got.last_action and got.last_action.transaction_ids == [11]
    assert await aredis.ttl("session:1") > 0
    await store.clear(1)
    assert (await store.get(1)).last_action is None


async def test_stream_queue_delivers_and_acks(aredis):
    q = RedisStreamQueue(aredis, consumer="t1")
    seen = []
    stop = asyncio.Event()

    async def handler(raw_id):
        seen.append(raw_id)
        if len(seen) == 2:
            stop.set()

    await q.enqueue(7)
    await q.enqueue(8)
    await asyncio.wait_for(q.run(handler, stop), timeout=5)
    assert seen == [7, 8]
    pending = await aredis.xpending(q.STREAM, q.GROUP)
    assert pending["pending"] == 0


async def test_stream_queue_failed_message_stays_pending(aredis):
    q = RedisStreamQueue(aredis, consumer="t2", claim_idle_ms=0)
    stop = asyncio.Event()
    calls = []

    async def flaky(raw_id):
        calls.append(raw_id)
        if len(calls) == 1:
            raise RuntimeError("worker died")
        stop.set()

    await q.enqueue(42)
    await asyncio.wait_for(q.run(flaky, stop), timeout=5)
    # first attempt failed and wasn't acked; it was reclaimed and retried
    assert calls == [42, 42]


async def test_inline_queue():
    q = InlineQueue()
    stop = asyncio.Event()
    got = []

    async def handler(i):
        got.append(i)
        if i == 2:
            stop.set()

    await q.enqueue(1)
    await q.enqueue(2)
    await asyncio.wait_for(q.run(handler, stop), timeout=3)
    assert got == [1, 2]


def test_quota_in_redis():
    r = fakeredis.FakeRedis(decode_responses=True)
    a = QuotaTracker({"m": {"rpm": 3}}, redis=r)
    b = QuotaTracker({"m": {"rpm": 3}}, redis=r)  # another process
    a.record("m", now=1000.0)
    b.record("m", now=1000.0)
    assert not a.near_limit("m", now=1000.0)
    a.record("m", now=1000.0)
    assert b.near_limit("m", now=1000.0)
    assert not b.near_limit("m", now=1070.0)  # next minute


def test_rate_limiter_memory_and_redis():
    for limiter in (RateLimiter(), RateLimiter(redis=fakeredis.FakeRedis())):
        assert all(limiter.hit("k", 3, 60, now=100.0 + i) for i in range(3))
        assert not limiter.hit("k", 3, 60, now=104.0)
        assert limiter.hit("k", 3, 60, now=200.0)
