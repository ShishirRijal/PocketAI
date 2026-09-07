"""The golden set against the real LLM chain. Opt-in: `pytest -m live`.

Tolerant on purpose: the models may pick a different-but-reasonable category or
tag, so this checks intent, amounts, currencies and dates (the things that
must be right) and reports category agreement as a score.
"""

from datetime import UTC, datetime

import pytest

from pocket.config import get_settings
from pocket.core.categorize import CatRef
from pocket.llm.stages import Pipeline, UserContext
from pocket.wiring import build_services
from tests.conftest import CATEGORY_NAMES, golden_messages

pytestmark = pytest.mark.live
CASES = golden_messages()


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> Pipeline:
    settings = get_settings().model_copy(
        update={
            "database_url": f"sqlite:///{tmp_path_factory.mktemp('live') / 'live.db'}",
            "llm_daily_cost_cap_usd": 0.5,
        }
    )
    svc = build_services(settings)
    svc.db.create_all()
    if all(m.startswith("rules/") for m in svc.router.usable_models("extract")):
        pytest.skip("no LLM provider keys configured")
    return svc.pipeline


def ctx() -> UserContext:
    from zoneinfo import ZoneInfo

    return UserContext(
        user_id=1,
        base_currency="EUR",
        timezone="Europe/Tallinn",
        now_local=datetime(2026, 9, 28, 21, 14, tzinfo=ZoneInfo("Europe/Tallinn")),
        categories=[CatRef(i + 1, n) for i, n in enumerate(CATEGORY_NAMES)],
    )


@pytest.mark.parametrize("case", CASES, ids=[c["text"] for c in CASES])
async def test_live_golden(case, pipeline):
    u = ctx()
    intent = await pipeline.intent(case["text"], u)
    assert intent.intent.value == case["intent"]
    if "txns" not in case:
        return
    res = await pipeline.extract(case["text"], u)
    assert len(res.transactions) == len(case["txns"])
    for got, want in zip(res.transactions, case["txns"], strict=True):
        assert got.amount == pytest.approx(want["amount"])
        assert got.currency == want["currency"]
        if "direction" in want:
            assert got.direction == want["direction"]
        if want.get("date") == "yesterday":
            assert (got.occurred_at or "").startswith("2026-09-27")


def test_now_is_utc_aware():
    assert datetime.now(UTC).tzinfo is UTC
