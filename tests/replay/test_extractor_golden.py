"""Golden set of messages -> expected parse. Runs every commit against the offline
rules parser. `pytest -m live` runs the same set against the real LLM chain."""

from datetime import date, timedelta

import pytest

from pocket.llm.rules import parser
from tests.conftest import golden_messages

TODAY = date(2026, 9, 28)
CASES = golden_messages()


def _check_txn(got, want):
    assert got.amount == pytest.approx(want["amount"])
    assert got.currency == want["currency"]
    if "category" in want:
        assert got.category_hint == want["category"]
    if "merchant" in want:
        assert got.merchant == want["merchant"]
    if "direction" in want:
        assert got.direction == want["direction"]
    for t in want.get("tags", []):
        assert t in {x.name for x in got.tags}
    if "date" in want:
        expected = {"today": TODAY, "yesterday": TODAY - timedelta(days=1)}[want["date"]]
        assert got.occurred_at == expected.isoformat()


@pytest.mark.parametrize("case", CASES, ids=[c["text"] for c in CASES])
def test_rules_golden(case, rules_ctx):
    intent = parser.classify_intent(case["text"])
    assert intent.intent.value == case["intent"]
    if "txns" in case:
        res = parser.extract(case["text"], rules_ctx)
        assert len(res.transactions) == len(case["txns"])
        for got, want in zip(res.transactions, case["txns"], strict=True):
            _check_txn(got, want)
    if "plan" in case:
        plan = parser.plan_query(case["text"], rules_ctx)
        for k, v in case["plan"].items():
            assert getattr(plan, k) == v, k
