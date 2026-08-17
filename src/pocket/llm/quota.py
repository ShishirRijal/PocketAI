"""Free-tier quota tracking.

We count requests per model per minute and per day and skip a model once it's
within 5% of a configured limit. Cheaper than eating the 429 plus the latency.
Counters live in Redis when available so the api and worker processes share them.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

HEADROOM = 0.95
EXHAUSTED_COOLDOWN_S = 60


class QuotaTracker:
    def __init__(self, limits: dict[str, dict[str, int]] | None = None, redis: Any = None):
        # limits: {"gemini/gemini-2.5-flash": {"rpm": 10, "rpd": 250}}
        self.limits = limits or {}
        self.redis = redis
        self._mem: dict[str, int] = defaultdict(int)
        self._exhausted_until: dict[str, float] = {}

    @staticmethod
    def _keys(model: str, now: float) -> tuple[str, str]:
        minute = int(now // 60)
        day = time.strftime("%Y%m%d", time.gmtime(now))
        return f"quota:{model}:m:{minute}", f"quota:{model}:d:{day}"

    def record(self, model: str, now: float | None = None) -> None:
        if model not in self.limits:
            return
        now = now or time.time()
        km, kd = self._keys(model, now)
        if self.redis is not None:
            pipe = self.redis.pipeline()
            pipe.incr(km)
            pipe.expire(km, 120)
            pipe.incr(kd)
            pipe.expire(kd, 60 * 60 * 26)
            pipe.execute()
        else:
            self._mem[km] += 1
            self._mem[kd] += 1

    def _get(self, key: str) -> int:
        if self.redis is not None:
            v = self.redis.get(key)
            return int(v) if v else 0
        return self._mem.get(key, 0)

    def usage(self, model: str, now: float | None = None) -> tuple[int, int]:
        now = now or time.time()
        km, kd = self._keys(model, now)
        return self._get(km), self._get(kd)

    def near_limit(self, model: str, now: float | None = None) -> bool:
        now = now or time.time()
        if self._exhausted_until.get(model, 0) > now:
            return True
        lim = self.limits.get(model)
        if not lim:
            return False
        per_min, per_day = self.usage(model, now)
        if "rpm" in lim and per_min >= lim["rpm"] * HEADROOM:
            return True
        return "rpd" in lim and per_day >= lim["rpd"] * HEADROOM

    def mark_exhausted(self, model: str, seconds: float = EXHAUSTED_COOLDOWN_S) -> None:
        """After a real 429, back off this model for a bit regardless of counters."""
        self._exhausted_until[model] = time.time() + seconds

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out = {}
        for model, lim in self.limits.items():
            m, d = self.usage(model)
            out[model] = {"rpm_used": m, "rpd_used": d, **lim}
        return out
