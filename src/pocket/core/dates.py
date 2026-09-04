"""Time helpers. Users think in local time; the DB stores UTC."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pocket.llm.rules.lexicon import MONTHS, WEEKDAYS


def local_now(tz: str, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(tz))


def local_midnight_utc(d: date, tz: str) -> datetime:
    return datetime.combine(d, time.min, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def resolve_occurred_at(value: str | None, tz: str, now: datetime | None = None) -> datetime:
    """LLM gives us an ISO date or datetime (local) or nothing. Returns aware UTC.

    - None           -> now
    - date only      -> today: now; other days: 12:00 local (a neutral time)
    - naive datetime -> interpreted as local
    - future         -> clamped to now (nobody logs tomorrow's coffee)
    """
    now_utc = now or datetime.now(UTC)
    if not value:
        return now_utc
    zone = ZoneInfo(tz)
    value = value.strip()
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            if d == now_utc.astimezone(zone).date():
                return now_utc
            dt = datetime.combine(d, time(12, 0), tzinfo=zone)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zone)
    except ValueError:
        return now_utc
    dt_utc = dt.astimezone(UTC)
    return min(dt_utc, now_utc)


_AGO = re.compile(r"\b(\d+)\s*(?:days?|din)\s*(?:ago|aghi|agadi)\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")\b")
_MONTH_DAY = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_ON_NTH = re.compile(r"\bon (?:the )?(\d{1,2})(?:st|nd|rd|th)\b")


def find_date(text: str, today: date) -> tuple[date | None, list[tuple[int, int]]]:
    """Pull a date reference out of free text. Returns (date, spans consumed)."""
    t = text.lower()

    def span(m: re.Match[str]) -> list[tuple[int, int]]:
        return [m.span()]

    if m := re.search(r"\b(day before yesterday|asti|parsi din)\b", t):
        return today - timedelta(days=2), span(m)
    if m := re.search(r"\b(yesterday|yday|hijo|hijoko|last night)\b", t):
        return today - timedelta(days=1), span(m)
    if m := re.search(r"\b(today|tonight|this morning|this evening|aja|aaja|ajako|aajako)\b", t):
        return today, span(m)
    if m := _AGO.search(t):
        return today - timedelta(days=int(m.group(1))), span(m)
    if m := _ISO.search(t):
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), span(m)
        except ValueError:
            pass
    for rx, order in ((_DAY_MONTH, "dm"), (_MONTH_DAY, "md")):
        if m := rx.search(t):
            a, b = m.group(1), m.group(2)
            day_s, mon_s = (a, b) if order == "dm" else (b, a)
            try:
                d = date(today.year, MONTHS[mon_s], int(day_s))
            except ValueError:
                continue
            if d > today:
                d = d.replace(year=d.year - 1)
            return d, span(m)
    if m := _ON_NTH.search(t):
        day_n = int(m.group(1))
        try:
            d = today.replace(day=day_n)
            if d > today:
                prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
                d = prev.replace(day=day_n)
            return d, span(m)
        except ValueError:
            pass
    wd_rx = r"\b(?:on |last )?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b"
    if m := re.search(wd_rx, t):
        target = WEEKDAYS[m.group(1)]
        delta = (today.weekday() - target) % 7
        if delta == 0 and m.group(0).startswith("last "):
            delta = 7
        return today - timedelta(days=delta), span(m)
    if m := _DMY.search(t):
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        try:
            year = today.year if not yy else (int(yy) + 2000 if len(yy) == 2 else int(yy))
            d = date(year, int(mm), int(dd))
            if d <= today:
                return d, span(m)
        except ValueError:
            pass
    return None, []


@dataclass
class Range:
    start: datetime  # aware UTC, inclusive
    end: datetime  # aware UTC, exclusive
    label: str
    so_far: bool = False  # period hasn't ended yet


def period_range(
    period: str,
    tz: str,
    now: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Range:
    ln = local_now(tz, now)
    today = ln.date()

    def rng(a: date, b: date, label: str, so_far: bool = False) -> Range:
        return Range(local_midnight_utc(a, tz), local_midnight_utc(b, tz), label, so_far)

    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    match period:
        case "today":
            return rng(today, today + timedelta(days=1), "Today", True)
        case "yesterday":
            y = today - timedelta(days=1)
            return rng(y, today, "Yesterday")
        case "this_week":
            return rng(week_start, week_start + timedelta(days=7), "This week", True)
        case "last_week":
            return rng(week_start - timedelta(days=7), week_start, "Last week")
        case "this_month":
            return rng(month_start, next_month, month_start.strftime("%B"), True)
        case "last_month":
            prev = (month_start - timedelta(days=1)).replace(day=1)
            return rng(prev, month_start, prev.strftime("%B %Y"))
        case "this_year":
            return rng(date(today.year, 1, 1), date(today.year + 1, 1, 1), str(today.year), True)
        case "last_year":
            return rng(date(today.year - 1, 1, 1), date(today.year, 1, 1), str(today.year - 1))
        case "last_7_days":
            return rng(today - timedelta(days=6), today + timedelta(days=1), "Last 7 days", True)
        case "last_90_days":
            return rng(today - timedelta(days=89), today + timedelta(days=1), "Last 90 days", True)
        case "last_30_days":
            return rng(today - timedelta(days=29), today + timedelta(days=1), "Last 30 days", True)
        case "all_time":
            return Range(
                datetime(2000, 1, 1, tzinfo=UTC),
                ln.astimezone(UTC) + timedelta(days=1),
                "All time",
                True,
            )
        case "custom":
            a = date.fromisoformat(start_date) if start_date else month_start
            b = date.fromisoformat(end_date) if end_date else today
            if b < a:
                a, b = b, a
            label = f"{a:%b %d} – {b:%b %d}" if a.year == b.year else f"{a} – {b}"
            return rng(a, b + timedelta(days=1), label, b >= today)
    raise ValueError(f"unknown period {period!r}")


def month_period(month: int, year: int | None, today: date) -> tuple[str, str]:
    """'september' -> custom range for that month (this year, or last year if it's ahead)."""
    y = year or (today.year if month <= today.month else today.year - 1)
    start = date(y, month, 1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def humanize_when(dt: datetime, tz: str, now: datetime | None = None) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    today = local_now(tz, now).date()
    if local.date() == today:
        return f"Today {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {local:%H:%M}"
    if (today - local.date()).days < 7:
        return f"{local:%a %H:%M}"
    if local.year == today.year:
        return f"{local:%b %d}"
    return f"{local:%Y-%m-%d}"
