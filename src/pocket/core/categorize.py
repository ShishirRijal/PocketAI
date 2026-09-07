"""Deterministic category matching. Runs before the LLM categorizer; most of the
time "grocery" -> "Groceries" doesn't need a model call."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pocket.llm.rules.lexicon import CATEGORY_KEYWORDS


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

    # the hint is a keyword we know belongs to a category the user has
    for cat_name, words in CATEGORY_KEYWORDS.items():
        if h in words or hs in words:
            kw_cat = by_lower.get(cat_name.lower())
            if kw_cat:
                return kw_cat, 0.9

    close = difflib.get_close_matches(h, list(by_lower), n=1, cutoff=0.82)
    if close:
        ratio = difflib.SequenceMatcher(None, h, close[0]).ratio()
        return by_lower[close[0]], round(ratio * 0.95, 3)
    return None, 0.0


def keyword_category(text: str) -> str | None:
    """Scan free text for a known keyword. Longest keyword wins so 'bolt food'
    beats 'bolt'."""
    t = f" {text.lower()} "
    best: tuple[int, str] | None = None
    for cat, words in CATEGORY_KEYWORDS.items():
        for w in words:
            if re.search(rf"(?<![\w-]){re.escape(w)}(?:e?s)?(?![\w-])", t) and (
                best is None or len(w) > best[0]
            ):
                best = (len(w), cat)
    return best[1] if best else None
