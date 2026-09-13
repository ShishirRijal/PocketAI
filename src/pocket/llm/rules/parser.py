"""Offline, deterministic stand-in for every LLM stage.

It's the last fallback in each chain (so an outage of every provider still
logs your coffee) and the default when no API keys are configured. It is good
at the common shapes ("23 eur groceries at rimi today", "sorry it was 29",
"how much grocery this month?") and honest about the rest via low confidence.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from pocket.core.categorize import CatRef, keyword_category, match_category
from pocket.core.dates import find_date, month_period
from pocket.llm.rules.lexicon import (
    CATEGORY_KEYWORDS,
    CURRENCY_WORDS,
    INCOME_WORDS,
    KNOWN_MERCHANTS,
    MONTHS,
    STOPWORDS,
    TRANSFER_WORDS,
)
from pocket.llm.schemas import (
    CategorizationResult,
    DeleteResolution,
    EditResolution,
    ExtractedTag,
    ExtractedTransaction,
    ExtractionResult,
    FieldChange,
    Intent,
    IntentResult,
    QueryPlan,
)

# ---------------------------------------------------------------- amounts

_CUR_ALTS = "|".join(re.escape(w) for w in sorted(CURRENCY_WORDS, key=len, reverse=True))
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_AMOUNT = re.compile(
    rf"(?:(?P<pre>{_CUR_ALTS})\s?)?"
    rf"(?<![\w.,/:])(?P<num>{_NUM})(?P<k>k\b)?"
    rf"(?:\s?(?P<post>{_CUR_ALTS})(?![a-z]))?",
    re.IGNORECASE,
)
# a number followed by one of these is not money
_NOT_MONEY_AFTER = re.compile(
    r"^\s*(?:am|pm|h\b|hrs?|hours?|mins?|minutes?|days?|din|weeks?|months?|years?|x\b|times|"
    r"st\b|nd\b|rd\b|th\b|%|:|kg|km|g\b|l\b|ml|people|persons?|pcs|pieces)",
    re.IGNORECASE,
)


@dataclass
class Amount:
    value: float
    currency: str | None  # None = bare number
    start: int
    end: int


def _to_float(num: str) -> float:
    num = num.replace(" ", "")
    if "," in num and "." in num:
        num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        # "6,50" is a decimal comma, "1,200" is thousands
        num = f"{head}.{tail}" if len(tail) <= 2 else num.replace(",", "")
    return float(num)


def find_amounts(text: str) -> list[Amount]:
    out: list[Amount] = []
    for m in _AMOUNT.finditer(text):
        pre, post = m.group("pre"), m.group("post")
        # pre-currency must be a symbol or a separate word ("rs 300", "€5"), not "eurs"
        cur_tok = (pre or post or "").lower()
        currency = CURRENCY_WORDS.get(cur_tok) if cur_tok else None
        if not currency and _NOT_MONEY_AFTER.match(text[m.end("num") :]):
            continue
        # "on 15th", "at 5", "#2", "no 3"
        before = text[max(0, m.start("num") - 4) : m.start("num")].lower()
        if not currency and re.search(r"(#|no\.?\s|number\s)$", before):
            continue
        try:
            v = _to_float(m.group("num"))
        except ValueError:
            continue
        if m.group("k"):
            v *= 1000
        if v <= 0:
            continue
        out.append(Amount(v, currency, m.start(), m.end()))
    # if some numbers carry a currency and some don't, the bare ones are probably
    # quantities ("2 coffees 7 eur")
    if any(a.currency for a in out) and not all(a.currency for a in out):
        with_cur = [a for a in out if a.currency]
        if len(with_cur) >= 1:
            out = with_cur
    return out


_SPLIT = re.compile(
    r"\s*(?:,|;|\+|\band then\b|\bthen\b|\band\b|\balso\b|\bani\b|\bra\b|&)\s*", re.IGNORECASE
)


def split_segments(text: str, amounts: list[Amount]) -> list[tuple[str, Amount]]:
    """One segment per amount, cut at the separator between consecutive amounts."""
    if len(amounts) <= 1:
        return [(text, amounts[0])] if amounts else []
    cuts = [0]
    for a, b in itertools.pairwise(amounts):
        between = text[a.end : b.start]
        seps = list(_SPLIT.finditer(between))
        # cut at the first separator after the amount: "6.50 and then metro 2" -> "and then"
        cuts.append(a.end + seps[0].start() if seps else a.end)
    cuts.append(len(text))
    segs = []
    for i, amt in enumerate(amounts):
        seg = text[cuts[i] : cuts[i + 1]]
        seg = _SPLIT.sub(" ", seg, count=1) if i > 0 else seg
        segs.append((seg.strip(" ,;+&"), amt))
    return segs


# ---------------------------------------------------------------- entities

_PERSON = re.compile(
    r"\b(?:with|w/|for)\s+([a-z][a-z'-]{1,20})\b|\b([a-z][a-z'-]{1,20})\s+(?:sanga|sang|saath)\b",
    re.IGNORECASE,
)
_AT = re.compile(r"(?:\bat|\bfrom|@|\bin)\s+([a-z0-9&'.-]+(?:\s+[a-z0-9&'.-]+)?)", re.IGNORECASE)
_NOT_PEOPLE = {
    "the",
    "a",
    "an",
    "my",
    "me",
    "work",
    "lunch",
    "dinner",
    "breakfast",
    "friends",
    "family",
    "kids",
    "home",
    "office",
    "coffee",
    "tea",
    "groceries",
    "grocery",
    "rent",
    "two",
    "three",
    "everyone",
    "team",
    "cash",
    "card",
    "him",
    "her",
    "them",
    "us",
    "you",
    "it",
    "that",
}


def _clean_words(text: str) -> list[str]:
    t = re.sub(_AMOUNT, " ", text)
    words = re.findall(r"[a-zA-ZÀ-ɏ][\w&'/-]*", t)
    return [w for w in words if w.lower() not in STOPWORDS and w.lower() not in CURRENCY_WORDS]


def find_people(text: str) -> list[str]:
    people = []
    for m in _PERSON.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        if name and name.lower() not in _NOT_PEOPLE and name.lower() not in STOPWORDS:
            # "for groceries" is not a person
            if keyword_category(name) or name.lower() in CURRENCY_WORDS:
                continue
            people.append(name.capitalize())
    return people


def find_merchant(text: str) -> str | None:
    low = f" {text.lower()} "
    for key in sorted(KNOWN_MERCHANTS, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", low):
            return KNOWN_MERCHANTS[key]
    for m in _AT.finditer(text):
        cand_words = [
            w
            for w in m.group(1).split()
            if w.lower() not in STOPWORDS
            and not re.match(r"^\d", w)
            and w.lower() not in CURRENCY_WORDS
        ]
        if not cand_words:
            continue
        cand = " ".join(cand_words)
        if keyword_category(cand) and cand.lower() in _all_keywords():
            continue  # "at lunch"
        if cand.lower() in _NOT_PEOPLE:
            continue
        return cand.title() if cand.islower() else cand
    return None


_KW_CACHE: set[str] | None = None


def _all_keywords() -> set[str]:
    global _KW_CACHE
    if _KW_CACHE is None:
        _KW_CACHE = {w for ws in CATEGORY_KEYWORDS.values() for w in ws}
    return _KW_CACHE


_LENDING = [
    (
        r"\b(paid me back|gave me back|returned (?:me|my)|got back|repaid me|settled up with me)\b|\bpaid back\b.*\bme\b",
        "got_back",
    ),
    (r"\b(paid back|repaid|returned)\b", "paid_back"),
    (r"\b(lent|lend|loaned|sapati diye|rin diye)\b|\bowes me\b", "lent"),
    (r"\b(borrowed|borrow|sapati liye|rin liye)\b|\bi owe\b", "borrowed"),
]
_LEND_PERSON = re.compile(
    r"\b(?:to|from|back)\s+([a-z][a-z'-]{1,20})\b|^\s*([a-z][a-z'-]{1,20})\s+(?:paid|returned|gave|owes|repaid)\b"
    r"|\bowe\s+([a-z][a-z'-]{1,20})\b|\b(?:lent|borrowed from|repaid|paid)\s+(?!back\b|me\b)([a-z][a-z'-]{1,20})\b",
    re.IGNORECASE,
)


def lending_person(text: str) -> str | None:
    for m in _LEND_PERSON.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if name and name.lower() not in _NOT_PEOPLE | STOPWORDS | {"back", "me", "him", "her"}:
            return name.capitalize()
    return None


def detect_direction(text: str) -> str:
    low = text.lower()
    for rx, direction in _LENDING:
        if re.search(rx, low):
            return direction
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in TRANSFER_WORDS):
        return "transfer"
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in INCOME_WORDS):
        return "income"
    return "expense"


# ---------------------------------------------------------------- stage: intent

_HELP = re.compile(
    r"^\s*(help|\?|h|commands|what can you do|how does this work|madat)\s*\??\s*$", re.I
)
_QUERY_START = re.compile(
    r"^\s*(how much|how many|how's|what|what's|whats|show|list|total|top|average|avg|sum|"
    r"where|which|when|kati|kun|kasari|spent|spending|breakdown|summary|summarize|compare|"
    r"biggest|largest|most)\b",
    re.I,
)
_QUERY_ANY = re.compile(
    r"\b(kati|how much|this month|last month|this week|so far)\b.*\?|\?\s*$", re.I
)
_DELETE = re.compile(
    r"\b(delete|remove|del|hatau|hatauna|hataideu|scrap|drop|erase|cancel)\b", re.I
)
_EDIT = re.compile(
    r"\b(sorry|actually|oops|change|edit|make it|should be|should've been|was actually|correct|"
    r"correction|fix|update|instead|it was|that was|not \d|wrong|meant|haina|hoina)\b",
    re.I,
)


def classify_intent(text: str) -> IntentResult:
    t = text.strip()
    if _HELP.match(t):
        return IntentResult(intent=Intent.HELP, confidence=0.99)
    amounts = find_amounts(t)
    if _QUERY_START.match(t) or (_QUERY_ANY.search(t) and not amounts):
        return IntentResult(intent=Intent.QUERY, confidence=0.9 if not amounts else 0.7)
    if _DELETE.search(t):
        return IntentResult(intent=Intent.DELETE, confidence=0.85)
    if _EDIT.search(t):
        return IntentResult(intent=Intent.EDIT, confidence=0.85 if amounts else 0.7)
    if amounts:
        return IntentResult(intent=Intent.ADD, confidence=0.9)
    if t.endswith("?"):
        return IntentResult(intent=Intent.QUERY, confidence=0.6)
    return IntentResult(intent=Intent.CHITCHAT, confidence=0.7)


# ---------------------------------------------------------------- stage: extract


def _ctx_today(ctx: dict[str, Any]) -> date:
    return date.fromisoformat(ctx["today"]) if ctx.get("today") else date.today()


def _cats(ctx: dict[str, Any]) -> list[CatRef]:
    return [CatRef(c["id"], c["name"]) for c in ctx.get("categories", [])]


def extract(text: str, ctx: dict[str, Any]) -> ExtractionResult:
    base = ctx.get("base_currency", "EUR")
    today = _ctx_today(ctx)
    d, date_spans = find_date(text, today)
    # blank out the date so "15th" or "2 days ago" doesn't read as money
    masked = text
    for a, b in date_spans:
        masked = masked[:a] + " " * (b - a) + masked[b:]
    amounts = find_amounts(masked)
    if not amounts:
        return ExtractionResult(transactions=[])

    # a currency mentioned anywhere applies to bare numbers ("coffee 3 and cake 4 eur")
    shared_cur = next((a.currency for a in amounts if a.currency), None)
    txns = []
    people_all = find_people(text)
    for seg, amt in split_segments(masked, amounts):
        currency = amt.currency or shared_cur or base
        merchant = find_merchant(seg)
        cat_hint = keyword_category(seg)
        people = find_people(seg) or (people_all if len(amounts) == 1 else [])
        direction = detect_direction(seg)
        if direction in ("lent", "borrowed", "got_back", "paid_back"):
            who = lending_person(seg) or lending_person(text)
            people = [who] if who else people
            cat_hint = None
            merchant = None
        if direction == "income" and not cat_hint:
            cat_hint = "Salary" if re.search(r"salary|talab|paycheck", seg, re.I) else "Income"
        words = [w for w in _clean_words(seg) if w.capitalize() not in people]
        if not cat_hint:
            # fall back to the user's own category names showing up in the text
            for w in words:
                c, score = match_category(w, _cats(ctx))
                if c and score >= 0.9:
                    cat_hint = c.name
                    break

        tags: list[ExtractedTag] = []
        if merchant:
            tags.append(ExtractedTag(name=merchant.lower(), kind="merchant"))
        for w in words:
            lw = w.lower()
            if lw in _all_keywords() and lw not in {t.name for t in tags}:
                tags.append(ExtractedTag(name=lw, kind="activity"))
        for p in people:
            tags.append(ExtractedTag(name=p.lower(), kind="person"))

        lending = direction in ("lent", "borrowed", "got_back", "paid_back")
        conf = 0.92
        if not amt.currency and not shared_cur:
            conf -= 0.05
        if lending:
            # no category by design; what matters is knowing who
            conf -= 0.0 if people else 0.3
        elif not cat_hint:
            conf -= 0.25
        if len(amounts) > 1:
            conf -= 0.05
        if not words and not merchant and not lending:
            conf -= 0.25  # "12" on its own: what was it?

        note = None
        if not cat_hint and not merchant and words:
            note = " ".join(words)[:120]
        elif people:
            note = "with " + ", ".join(people)

        txns.append(
            ExtractedTransaction(
                amount=round(amt.value, 4),
                currency=currency,
                direction=direction,
                merchant=merchant,
                category_hint=cat_hint,
                tags=tags,
                occurred_at=d.isoformat() if d else None,
                note=note,
                confidence=max(0.2, round(conf, 2)),
                reasoning="rules: " + ("keyword category" if cat_hint else "no category keyword"),
            )
        )
    return ExtractionResult(transactions=txns)


# ---------------------------------------------------------------- stage: categorize


def categorize(ctx: dict[str, Any]) -> CategorizationResult:
    cats = _cats(ctx)
    hint = ctx.get("category_hint")
    text = ctx.get("text", "")
    c, score = match_category(hint, cats)
    if c:
        return CategorizationResult(
            category_id=c.id, confidence=min(0.95, score), rationale="rules match"
        )
    kw = keyword_category(text)
    c, score = match_category(kw, cats)
    if c:
        return CategorizationResult(category_id=c.id, confidence=0.8, rationale="keyword")
    if hint:
        return CategorizationResult(
            new_category_name=hint.strip().title()[:40],
            confidence=0.5,
            rationale="no existing category matched the hint",
        )
    misc = next((x for x in cats if x.name.lower() in ("miscellaneous", "misc", "other")), None)
    return CategorizationResult(
        category_id=misc.id if misc else None, confidence=0.4, rationale="nothing to go on"
    )


# ---------------------------------------------------------------- stage: edit/delete

_ORDINAL = {
    "last": 1,
    "latest": 1,
    "that": 1,
    "it": 1,
    "this": 1,
    "previous": 2,
    "second last": 2,
    "second": 2,
    "third": 3,
    "2nd": 2,
    "3rd": 3,
    "4th": 4,
    "5th": 5,
    "first": 1,
}


def _target_from_text(text: str, recent: list[dict[str, Any]]) -> tuple[int | None, float]:
    low = text.lower()
    if m := re.search(r"(?:#|number |no\.? ?)(\d)\b", low):
        return int(m.group(1)), 0.95
    if re.search(r"\b(second last|previous one|one before)\b", low):
        return 2, 0.85
    # mention of a merchant / category / tag of a recent txn
    best: tuple[int, float] | None = None
    for r in recent:
        hay = [r.get("merchant"), r.get("category"), *(r.get("tags") or [])]
        for h in hay:
            if h and re.search(rf"\b{re.escape(str(h).lower())}\b", low):
                if best is None:
                    best = (r["index"], 0.85)
                break
    if best:
        return best
    return (1, 0.8) if recent else (None, 0.0)


def resolve_edit(text: str, ctx: dict[str, Any]) -> EditResolution:
    recent = ctx.get("recent", [])
    target, conf = _target_from_text(text, recent)
    low = text.lower()
    changes: list[FieldChange] = []

    masked = re.sub(r"(?:#|number |no\.? ?)\d\b", " ", low)
    d, spans = find_date(masked, _ctx_today(ctx))
    for a, b in spans:
        masked = masked[:a] + " " * (b - a) + masked[b:]
    amounts = find_amounts(masked)
    if amounts:
        # "not 23, 29" / "23 -> 29": the last number is the new value
        new = amounts[-1]
        changes.append(FieldChange(field="amount", value=f"{new.value:g}"))
        if new.currency:
            changes.append(FieldChange(field="currency", value=new.currency))
    elif m := re.search(rf"\b(?:in|to)\s+({_CUR_ALTS})\b", low):
        changes.append(FieldChange(field="currency", value=CURRENCY_WORDS[m.group(1)]))
    if d:
        changes.append(FieldChange(field="date", value=d.isoformat()))

    cat_m = re.search(
        r"\b(?:category|cat)\s*(?:to|as|is|=|:)?\s*([a-z][\w /&-]+)$|\b(?:it was|was|should be|"
        r"make it|move to|put (?:it )?(?:in|under))\s+([a-z][\w /&-]+)$",
        low,
    )
    if cat_m:
        name = (cat_m.group(1) or cat_m.group(2)).strip()
        c, score = match_category(name, _cats(ctx))
        kw = keyword_category(name)
        if c or kw or "category" in low:
            changes.append(
                FieldChange(field="category", value=c.name if c else (kw or name.title()))
            )
    if m := re.search(r"\b(?:merchant|shop|store|place)\s*(?:to|as|is|=|:)?\s*([\w &'-]+)$", low):
        changes.append(FieldChange(field="merchant", value=m.group(1).strip().title()))
    elif (m := re.search(r"\bat\s+([a-z][\w&'-]+)", low)) and not any(
        c.field == "merchant" for c in changes
    ):
        cand = m.group(1)
        if cand not in STOPWORDS:
            changes.append(
                FieldChange(field="merchant", value=KNOWN_MERCHANTS.get(cand, cand.title()))
            )
    if m := re.search(r"\bnote\s*(?:to|as|is|=|:)?\s*(.+)$", text, re.I):
        changes.append(FieldChange(field="note", value=m.group(1).strip()))
    if re.search(r"\b(it was|was|make it|as)\s+(an?\s+)?income\b", low):
        changes.append(FieldChange(field="direction", value="income"))

    if not changes:
        conf = min(conf, 0.3)
    return EditResolution(
        target_index=target, changes=changes, confidence=conf, reasoning="rules edit resolver"
    )


def resolve_delete(text: str, ctx: dict[str, Any]) -> DeleteResolution:
    recent = ctx.get("recent", [])
    low = text.lower()
    if m := re.search(r"\blast (two|three|2|3)\b", low):
        n = {"two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        return DeleteResolution(target_indexes=list(range(1, n + 1)), confidence=0.85)
    nums = [int(n) for n in re.findall(r"(?<![\d.])([1-9])(?![\d.])", low)]
    if nums and all(1 <= n <= max(len(recent), 1) for n in nums):
        return DeleteResolution(target_indexes=nums, confidence=0.9)
    target, conf = _target_from_text(text, recent)
    return DeleteResolution(
        target_indexes=[target] if target else [], confidence=conf, reasoning="rules delete"
    )


# ---------------------------------------------------------------- stage: query

_PERIODS = [
    (r"\btoday|\baja\b|\baaja\b", "today"),
    (r"\byesterday|\bhijo\b", "yesterday"),
    (r"\blast week|\bgako hapta|\bpachhillo hapta", "last_week"),
    (r"\bthis week|\byo hapta|\bweek\b|\bhapta", "this_week"),
    (r"\blast month|\bgako mahina|\bpachhillo mahina", "last_month"),
    (r"\bthis month|\byo mahina|\bmonth\b|\bmahina", "this_month"),
    (r"\blast year|\bgako barsa", "last_year"),
    (r"\bthis year|\byo barsa|\byear\b", "this_year"),
    (r"\blast 7 days|\bpast 7 days|\bpast week", "last_7_days"),
    (r"\blast 30 days|\bpast 30 days|\bpast month", "last_30_days"),
    (r"\ball time|\bever\b|\bin total\b|\boverall", "all_time"),
]


def plan_query(text: str, ctx: dict[str, Any]) -> QueryPlan:
    low = text.lower().strip()
    today = _ctx_today(ctx)
    period, start, end = "this_month", None, None
    for rx, p in _PERIODS:
        if re.search(rx, low):
            period = p
            break
    else:
        month_rx = r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b(?:\s+(\d{4}))?"
        if (m := re.search(month_rx, low)) and m.group(1) not in ("may", "mar", "sun"):
            period = "custom"
            start, end = month_period(
                MONTHS[m.group(1)], int(m.group(2)) if m.group(2) else None, today
            )

    kind = "total"
    group_by = None
    if re.search(r"\btop merchants?|\bwhere .* (most|spend)|\bmerchants\b|\bshops\b", low):
        kind = "top_merchants"
    elif re.search(r"\btop categor|\bby category|\bbreakdown|\bcategories\b|\bsplit\b", low):
        kind, group_by = "breakdown", "category"
    elif re.search(r"\btop tags|\bby tag", low):
        kind = "top_tags"
    elif re.search(r"\bper day|\bdaily average|\ba day\b|\bdaily\b", low):
        kind = "daily_average"
    elif re.search(r"\baverage|\bavg|\bmean\b|\bausat", low):
        kind = "average"
    elif re.search(r"\bhow many|\bcount|\bnumber of|\bkati ota|\bkati choti", low):
        kind = "count"
    elif re.search(r"\bbiggest|\blargest|\bmost expensive|\bhighest", low):
        kind = "largest"
    elif re.search(r"^\s*(show|list)\b|\btransactions\b|\bwhat did i (buy|spend on)", low):
        kind = "list"
    elif re.search(r"\bby day\b|\beach day\b", low):
        kind, group_by = "breakdown", "day"
    elif re.search(r"\bby weekday\b", low):
        kind, group_by = "breakdown", "weekday"
    elif re.search(r"\bby month\b|\bmonthly\b", low):
        kind, group_by = "breakdown", "month"

    direction = "expense"
    if re.search(r"\b(earn|earned|income|made|received|salary)\b", low):
        direction = "income"

    cat_name = None
    cats = _cats(ctx)
    for w in re.findall(r"[a-z][\w&-]+", low):
        if w in STOPWORDS or w in {"spend", "spent", "much", "how", "month", "week", "year"}:
            continue
        c, score = match_category(w, cats)
        if c and score >= 0.85:
            cat_name = c.name
            break
    if not cat_name:
        kw = keyword_category(low)
        if kw and any(c.name == kw for c in cats):
            cat_name = kw

    merchant = None
    for key in sorted(KNOWN_MERCHANTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            merchant = KNOWN_MERCHANTS[key]
            break
    if merchant and cat_name and kind == "total" and re.search(rf"\bat {merchant.lower()}", low):
        cat_name = None  # "how much at rimi" is about the merchant

    tag = None
    people = find_people(low)
    if people:
        tag = people[0].lower()

    limit = 5
    if m := re.search(r"\btop (\d{1,2})\b|\blast (\d{1,2}) (?:transactions|txns|items)", low):
        limit = int(m.group(1) or m.group(2))
        if m.group(2):
            kind, period = "list", "all_time"

    return QueryPlan(
        kind=kind,
        period=period,
        start_date=start,
        end_date=end,
        category=cat_name,
        merchant=merchant,
        tag=tag,
        direction=direction,
        group_by=group_by,
        weekdays_only=bool(re.search(r"\bweekdays?\b", low)),
        weekends_only=bool(re.search(r"\bweekends?\b", low)),
        limit=limit,
        confidence=0.75,
    )
