"""Deterministic checks on LLM output. The model proposes; these catch the
cheap-to-detect mistakes before anything is saved."""

from __future__ import annotations

from pocket.llm.rules.parser import find_amounts
from pocket.llm.schemas import ExtractionResult


def currency_guard(text: str, result: ExtractionResult) -> ExtractionResult:
    """If the message spells out a currency next to an amount ("£8", "₹450",
    "30 npr"), that beats whatever the model said. Only applied when the
    amounts line up one-to-one, so we never guess which amount is which."""
    explicit = [a for a in find_amounts(text) if a.currency]
    if not explicit or not result.transactions:
        return result
    by_value: dict[float, str] = {}
    for a in explicit:
        by_value.setdefault(round(a.value, 2), a.currency)  # type: ignore[arg-type]
    for t in result.transactions:
        cur = by_value.get(round(t.amount, 2))
        if cur and cur != t.currency:
            t.currency = cur
            t.reasoning = (t.reasoning + f" [currency corrected to {cur} from the text]").strip()
    return result
