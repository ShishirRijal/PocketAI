"""Deterministic checks on LLM output. The model proposes; these catch the
cheap-to-detect mistakes before anything is saved.

They don't parse the message into transactions (that's the LLM's job). They only
look for numbers and explicit currency marks in the text to double-check what
the model returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pocket.llm.schemas import ExtractionResult

# explicit currency marks only: symbols, ISO codes and unambiguous words
_CURRENCY = {
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP",
    "₨": "NPR", "npr": "NPR", "rs": "NPR", "rs.": "NPR", "rupees": "NPR", "रु": "NPR", "रू": "NPR",
    "₹": "INR", "inr": "INR", "sek": "SEK", "nok": "NOK", "dkk": "DKK", "chf": "CHF",
    "pln": "PLN", "czk": "CZK", "jpy": "JPY", "¥": "JPY",
}  # fmt: skip
_CUR = "|".join(re.escape(w) for w in sorted(_CURRENCY, key=len, reverse=True))
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(
    rf"(?:(?P<pre>{_CUR})\s?)?(?<![\w.,/:])(?P<num>{_NUM})(?P<k>k\b)?(?:\s?(?P<post>{_CUR})(?![a-z]))?",
    re.IGNORECASE,
)


@dataclass
class _Found:
    value: float
    currency: str | None


def _to_float(num: str) -> float:
    num = num.replace(" ", "")
    if "," in num and "." in num:
        num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = f"{head}.{tail}" if len(tail) <= 2 else num.replace(",", "")
    return float(num)


def numbers_in(text: str) -> list[_Found]:
    """Every number in the text, with an explicit currency if one is attached."""
    out = []
    for m in _AMOUNT.finditer(text):
        tok = (m.group("pre") or m.group("post") or "").lower()
        try:
            v = _to_float(m.group("num")) * (1000 if m.group("k") else 1)
        except ValueError:
            continue
        out.append(_Found(v, _CURRENCY.get(tok) if tok else None))
    return out


def currency_guard(text: str, result: ExtractionResult) -> ExtractionResult:
    """If the message spells out a currency next to an amount ("£8", "₹450",
    "30 npr"), that beats whatever the model said. Only applied when an amount
    matches exactly, so we never guess which amount is which."""
    by_value: dict[float, str] = {}
    for f in numbers_in(text):
        if f.currency:
            by_value.setdefault(round(f.value, 2), f.currency)
    for t in result.transactions:
        cur = by_value.get(round(t.amount, 2))
        if cur and cur != t.currency:
            t.currency = cur
            t.reasoning = (t.reasoning + f" [currency corrected to {cur} from the text]").strip()
    return result


def amount_guard(text: str, result: ExtractionResult, *, cap: float = 0.5) -> ExtractionResult:
    """ "Never invent amounts": when the message has numbers but a proposed
    amount isn't one of them, cap its confidence so policy asks first.
    Messages without digits ("twelve euro lunch") can't be checked this way
    and are left alone."""
    if not any(ch.isdigit() for ch in text):
        return result
    seen = {round(f.value, 2) for f in numbers_in(text)}
    for t in result.transactions:
        if round(t.amount, 2) not in seen and t.confidence > cap:
            t.confidence = cap
            t.reasoning = (t.reasoning + " [amount not found in the message]").strip()
    return result
