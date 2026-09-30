"""Map a category *name* (the LLM's suggestion, or what you typed) onto one of
your existing categories: exact, singular/plural, or a close typo. This is what
stops "Grocery", "Groceries" and "Grocceries" from becoming three categories.

It never reads your message; understanding the message is the LLM's job."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class CatRef:
    id: int
    name: str


def _stem(word: str) -> str:
    w = word.lower().strip()
    w = re.sub(r"[^a-z0-9/ &-]", "", w)
    for suf in ("ies", "es", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def match_category(hint: str | None, categories: Sequence[CatRef]) -> tuple[CatRef | None, float]:
    """Match a free-text hint to one of the user's categories.

    Returns (category, score). score 1.0 = exact/stem match, lower = fuzzy.
    """
    if not hint:
        return None, 0.0
    h = hint.strip().lower()
    by_lower = {c.name.lower(): c for c in categories}
    # "Food/Groceries" style: match the leaf too
    leaf = {c.name.lower().rsplit("/", 1)[-1]: c for c in categories}

    if h in by_lower:
        return by_lower[h], 1.0
    if h in leaf:
        return leaf[h], 1.0
    hs = _stem(h)
    for name, c in {**by_lower, **leaf}.items():
        if _stem(name) == hs:
            return c, 0.97

    close = difflib.get_close_matches(h, list(by_lower), n=1, cutoff=0.82)
    if close:
        ratio = difflib.SequenceMatcher(None, h, close[0]).ratio()
        return by_lower[close[0]], round(ratio * 0.95, 3)
    return None, 0.0
