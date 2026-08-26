"""FX rates with a cache.

Sources, in order:
1. frankfurter (ECB reference rates, free, no key). ECB doesn't publish NPR, but
   NPR is pegged to INR at 1.6, so NPR goes through INR.
2. open.er-api.com (free, no key, has NPR directly).
3. A small static table so logging never fails offline. Marked stale in the reply.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

NPR_PER_INR = Decimal("1.6")

# rough EUR-based rates, only used when every API is down. Better than dropping data.
STATIC_EUR_RATES = {
    "EUR": Decimal("1"),
    "USD": Decimal("1.14"),
    "GBP": Decimal("0.86"),
    "INR": Decimal("109"),
    "NPR": Decimal("174.4"),
    "SEK": Decimal("11"),
    "NOK": Decimal("11.7"),
    "DKK": Decimal("7.46"),
    "CHF": Decimal("0.94"),
    "PLN": Decimal("4.25"),
    "CZK": Decimal("24.5"),
    "JPY": Decimal("168"),
}


@dataclass
class Rate:
    rate: Decimal  # units of `to` per 1 `from`
    source: str
    fetched_at: float

    def age_hours(self) -> float:
        return (time.time() - self.fetched_at) / 3600


class FxError(Exception):
    pass


class FxService:
    def __init__(
        self,
        cache_hours: float = 12,
        client: httpx.AsyncClient | None = None,
        redis=None,
    ):
        self.cache_s = cache_hours * 3600
        self.client = client or httpx.AsyncClient(timeout=6)
        self.redis = redis
        self._cache: dict[str, tuple[dict[str, Decimal], str, float]] = {}

    async def rate(self, from_cur: str, to_cur: str) -> Rate:
        from_cur, to_cur = from_cur.upper(), to_cur.upper()
        if from_cur == to_cur:
            return Rate(Decimal(1), "identity", time.time())
        table, source, fetched = await self._table(to_cur)
        if from_cur not in table or table[from_cur] == 0:
            raise FxError(f"no rate for {from_cur}->{to_cur}")
        # table is "units of X per 1 base(to_cur)", so invert
        return Rate((Decimal(1) / table[from_cur]).quantize(Decimal("1e-10")), source, fetched)

    async def _table(self, base: str) -> tuple[dict[str, Decimal], str, float]:
        hit = self._cache.get(base)
        if hit and time.time() - hit[2] < self.cache_s:
            return hit
        if self.redis is not None:
            try:
                raw = await self.redis.hgetall(f"fx:{base}")
                if raw and time.time() - float(raw.get("_fetched", 0)) < self.cache_s:
                    rates = {k: Decimal(v) for k, v in raw.items() if not k.startswith("_")}
                    hit = (rates, raw.get("_source", "cache"), float(raw["_fetched"]))
                    self._cache[base] = hit
                    return hit
            except Exception:
                log.warning("fx redis cache read failed", exc_info=True)

        for fetch in (self._frankfurter, self._open_er):
            try:
                rates, source = await fetch(base)
                entry = (rates, source, time.time())
                self._cache[base] = entry
                await self._store(base, entry)
                return entry
            except Exception as e:
                log.warning("fx source %s failed: %s", fetch.__name__, e)
        if hit:  # stale cache beats static table
            return hit
        return self._static(base), "static", time.time() - 86400 * 30

    async def _store(self, base: str, entry: tuple[dict[str, Decimal], str, float]) -> None:
        if self.redis is None:
            return
        rates, source, fetched = entry
        try:
            mapping = {k: str(v) for k, v in rates.items()}
            mapping.update({"_source": source, "_fetched": str(fetched)})
            await self.redis.hset(f"fx:{base}", mapping=mapping)
            await self.redis.expire(f"fx:{base}", int(self.cache_s * 2))
        except Exception:
            log.warning("fx redis cache write failed", exc_info=True)

    async def _frankfurter(self, base: str) -> tuple[dict[str, Decimal], str]:
        ecb_base = "INR" if base == "NPR" else base
        r = await self.client.get(
            "https://api.frankfurter.dev/v1/latest", params={"base": ecb_base}
        )
        r.raise_for_status()
        data = r.json()
        rates = {k: Decimal(str(v)) for k, v in data["rates"].items()}
        rates[ecb_base] = Decimal(1)
        if base == "NPR":
            # everything is per 1 INR; per 1 NPR is that / 1.6
            rates = {k: v / NPR_PER_INR for k, v in rates.items()}
            rates["NPR"] = Decimal(1)
        elif "INR" in rates:
            rates["NPR"] = rates["INR"] * NPR_PER_INR
        return rates, f"ECB {data.get('date', '')}".strip()

    async def _open_er(self, base: str) -> tuple[dict[str, Decimal], str]:
        r = await self.client.get(f"https://open.er-api.com/v6/latest/{base}")
        r.raise_for_status()
        data = r.json()
        if data.get("result") != "success":
            raise FxError(data.get("error-type", "unknown"))
        return {k: Decimal(str(v)) for k, v in data["rates"].items()}, "open.er-api"

    @staticmethod
    def _static(base: str) -> dict[str, Decimal]:
        if base not in STATIC_EUR_RATES:
            raise FxError(f"no static rates for base {base}")
        per_base = STATIC_EUR_RATES[base]
        return {k: v / per_base for k, v in STATIC_EUR_RATES.items()}

    async def aclose(self) -> None:
        await self.client.aclose()
