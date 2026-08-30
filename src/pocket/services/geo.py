"""Reverse geocoding for WhatsApp/Telegram location pins -> city tag."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def reverse_city(
    lat: float, lon: float, client: httpx.AsyncClient | None = None
) -> str | None:
    """OpenStreetMap Nominatim. Free, 1 req/s policy, needs a User-Agent."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=5)
    try:
        r = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10},
            headers={"User-Agent": "pocket-finance-bot/0.1 (personal use)"},
        )
        r.raise_for_status()
        addr = r.json().get("address", {})
        return (
            addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
        )
    except Exception as e:
        log.warning("reverse geocode failed: %s", e)
        return None
    finally:
        if own:
            await client.aclose()
