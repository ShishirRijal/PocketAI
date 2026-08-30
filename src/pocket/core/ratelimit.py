"""Sliding-window rate limiter. Redis sorted sets when available, memory otherwise."""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from typing import Any


class RateLimiter:
    def __init__(self, redis: Any = None):
        self.redis = redis
        self._mem: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_s: float, now: float | None = None) -> bool:
        """Record a hit; True if still within the limit."""
        now = now or time.time()
        if self.redis is not None:
            k = f"rl:{key}"
            pipe = self.redis.pipeline()
            pipe.zremrangebyscore(k, 0, now - window_s)
            pipe.zadd(k, {f"{now}:{uuid.uuid4().hex[:6]}": now})
            pipe.zcard(k)
            pipe.expire(k, int(window_s) + 1)
            _, _, count, _ = pipe.execute()
            return int(count) <= limit
        q = self._mem[key]
        while q and q[0] <= now - window_s:
            q.popleft()
        q.append(now)
        return len(q) <= limit

    def scoped(self, limit: int, window_s: float) -> ScopedLimiter:
        return ScopedLimiter(self, limit, window_s)


class ScopedLimiter:
    def __init__(self, parent: RateLimiter, limit: int, window_s: float):
        self.parent = parent
        self.limit = limit
        self.window_s = window_s

    def allow(self, key: str) -> bool:
        return self.parent.hit(key, self.limit, self.window_s)
