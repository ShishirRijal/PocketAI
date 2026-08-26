from decimal import Decimal

import httpx
import respx

from pocket.services.fx import FxService


def frankfurter(base, rates, date="2026-09-25"):
    return httpx.Response(200, json={"amount": 1, "base": base, "date": date, "rates": rates})


@respx.mock
async def test_npr_via_inr_peg():
    respx.get("https://api.frankfurter.dev/v1/latest").mock(
        return_value=frankfurter("EUR", {"INR": 109.0, "USD": 1.14})
    )
    fx = FxService()
    r = await fx.rate("NPR", "EUR")
    # 1 EUR = 109 INR = 174.4 NPR
    assert abs(r.rate - Decimal(1) / Decimal("174.4")) < Decimal("1e-9")
    assert r.source.startswith("ECB")


@respx.mock
async def test_npr_base_currency():
    respx.get("https://api.frankfurter.dev/v1/latest").mock(
        return_value=frankfurter("INR", {"EUR": 0.00917, "USD": 0.0105})
    )
    fx = FxService()
    r = await fx.rate("EUR", "NPR")
    assert abs(r.rate - Decimal("174.48")) < Decimal("0.1")


@respx.mock
async def test_cached():
    route = respx.get("https://api.frankfurter.dev/v1/latest").mock(
        return_value=frankfurter("EUR", {"USD": 1.14})
    )
    fx = FxService()
    await fx.rate("USD", "EUR")
    await fx.rate("USD", "EUR")
    assert route.call_count == 1


@respx.mock
async def test_falls_back_to_open_er_then_static():
    respx.get("https://api.frankfurter.dev/v1/latest").mock(return_value=httpx.Response(503))
    er = respx.get("https://open.er-api.com/v6/latest/EUR").mock(
        return_value=httpx.Response(200, json={"result": "success", "rates": {"NPR": 175.0}})
    )
    fx = FxService()
    r = await fx.rate("NPR", "EUR")
    assert r.source == "open.er-api" and er.called

    er.mock(return_value=httpx.Response(500))
    fx2 = FxService()
    r2 = await fx2.rate("USD", "EUR")
    assert r2.source == "static"


async def test_identity():
    assert (await FxService().rate("EUR", "eur")).rate == 1
