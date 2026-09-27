"""Deterministic command parsing. Anything matched here never touches an LLM:
it's instant, free, and works during a provider outage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Command:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


FIELD_ALIASES = {
    "amount": "amount",
    "amt": "amount",
    "price": "amount",
    "cat": "category",
    "category": "category",
    "merchant": "merchant",
    "shop": "merchant",
    "store": "merchant",
    "note": "note",
    "memo": "note",
    "date": "date",
    "when": "date",
    "day": "date",
    "currency": "currency",
    "cur": "currency",
    "tags": "tags",
    "tag": "tags",
    "type": "direction",
    "direction": "direction",
}

_PERIOD_WORDS = {
    "today": "today",
    "yesterday": "yesterday",
    "week": "this_week",
    "this week": "this_week",
    "last week": "last_week",
    "month": "this_month",
    "this month": "this_month",
    "last month": "last_month",
    "year": "this_year",
    "this year": "this_year",
    "all": "all_time",
    "all time": "all_time",
}

YES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "sure",
    "save",
    "confirm",
    "ha",
    "haa",
    "hunchha",
    "hus",
    "huncha",
    "👍",
    "✅",
    "correct",
    "right",
    "do it",
    "go",
}
NO = {
    "no",
    "n",
    "nope",
    "nah",
    "cancel",
    "stop",
    "hoina",
    "haina",
    "pardaina",
    "❌",
    "skip",
    "drop",
    "discard",
    "nevermind",
    "never mind",
}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".!")


def is_yes(text: str) -> bool:
    return norm(text) in YES


def is_no(text: str) -> bool:
    return norm(text) in NO


def pick_numbers(text: str, upper: int) -> list[int] | None:
    """'1', '1,2', '1 and 3', '2 3' -> indexes, if that's all the message is."""
    t = norm(text)
    if not re.fullmatch(r"\d+(?:\s*(?:,|and|&|\s)\s*\d+)*", t):
        return None
    nums = [int(n) for n in re.findall(r"\d+", t)]
    if all(1 <= n <= upper for n in nums):
        return sorted(set(nums))
    return None


def _period(words: str) -> str | None:
    return _PERIOD_WORDS.get(words.strip())


def parse(text: str) -> Command | None:
    t = norm(text)
    # telegram/discord style "/week" or "/undo@PocketBot"
    t = re.sub(r"^/(\w+)(?:@\w+)?", r"\1", t)
    if not t:
        return None

    if t in {"undo", "u", "undo that", "undo last", "revert"}:
        return Command("undo")
    if t in {"help", "?", "h", "commands", "/help", "/start", "start"}:
        return Command("help")
    if t in {"edit", "e", "recent", "last", "list", "ls", "r"}:
        return Command("recent")
    if m := re.fullmatch(r"(?:recent|list|last|ls)\s+(\d{1,2})", t):
        return Command("recent", {"n": int(m.group(1))})

    # edit N field value | edit last field [to] value
    if m := re.fullmatch(
        r"(?:edit|e|change|fix)\s+(\d|last)\s+(\w+)\s+(?:to\s+|=\s*|as\s+)?(.+)", t
    ):
        f = FIELD_ALIASES.get(m.group(2))
        if f:
            idx = 1 if m.group(1) == "last" else int(m.group(1))
            # keep the original casing of the value (notes, merchant names)
            raw_value = re.split(r"\s+", text.strip(), maxsplit=3)
            value = m.group(3)
            if len(raw_value) == 4:
                value = re.sub(r"^(?:to\s+|=\s*|as\s+)", "", raw_value[3], flags=re.I)
            return Command("edit", {"index": idx, "field": f, "value": value})
    # edit 2 29  (bare value = amount)
    if m := re.fullmatch(r"(?:edit|e)\s+(\d|last)\s+(\d+(?:[.,]\d{1,2})?)", t):
        idx = 1 if m.group(1) == "last" else int(m.group(1))
        return Command("edit", {"index": idx, "field": "amount", "value": m.group(2)})

    if t in {"delete", "del", "d", "remove", "rm"}:
        return Command("recent", {"for": "delete"})
    if m := re.fullmatch(r"(?:delete|del|d|remove|rm)\s+(\d(?:\s*(?:,|and|&|\s)\s*\d)*)", t):
        return Command(
            "delete", {"indexes": sorted({int(n) for n in re.findall(r"\d", m.group(1))})}
        )
    if t in {"delete last", "del last", "delete that", "remove last", "d last"}:
        return Command("delete", {"indexes": [1]})

    if m := re.fullmatch(r"(?:show|s)(?:\s+(.+))?", t):
        what = (m.group(1) or "today").strip()
        if p := _period(what):
            return Command("show", {"period": p})
    if t in {
        "today",
        "this week",
        "this month",
        "last month",
        "last week",
        "yesterday",
        "week",
        "month",
    }:
        return Command("show", {"period": _PERIOD_WORDS[t]})

    if m := re.fullmatch(r"cost(?:\s+(.+))?", t):
        return Command("cost", {"period": _period(m.group(1) or "") or "last_7_days"})

    if t in {"categories", "cats", "category", "category list"}:
        return Command("categories")
    if m := re.fullmatch(r"(?:category|cat)\s+(?:add|new|create)\s+(.+)", t):
        return Command("category_add", {"name": _orig_tail(text, m.group(1))})
    if m := re.fullmatch(r"(?:category|cat)\s+rename\s+(.+?)\s+(?:to|->|=>)\s+(.+)", t):
        return Command("category_rename", {"old": m.group(1), "new": _orig_tail(text, m.group(2))})
    if m := re.fullmatch(r"(?:category|cat)\s+(?:archive|remove|delete|hide)\s+(.+)", t):
        return Command("category_archive", {"name": m.group(1)})
    if m := re.fullmatch(r"(?:category|cat)\s+merge\s+(.+?)\s+(?:into|to|->)\s+(.+)", t):
        return Command("category_merge", {"src": m.group(1), "dst": m.group(2)})

    if t in {"tags", "tag list"}:
        return Command("tags")

    if t in {"budgets", "budget", "budget list"}:
        return Command("budgets")
    if m := re.fullmatch(r"budget\s+(?:remove|delete|clear|off)\s+(.+)", t):
        return Command("budget_remove", {"category": m.group(1)})
    if m := re.fullmatch(
        r"budget\s+(.+?)\s+(\d+(?:[.,]\d{1,2})?)(?:\s*(?:/|per|a)?\s*(month|week|mo|wk))?", t
    ):
        period = "week" if (m.group(3) or "").startswith("w") else "month"
        return Command(
            "budget_set",
            {"category": m.group(1), "amount": m.group(2).replace(",", "."), "period": period},
        )

    if t in {"recurring", "subscriptions", "recurring list", "repeats"}:
        return Command("recurring")
    if m := re.fullmatch(r"(?:recurring|repeat)\s+(?:remove|delete|stop|cancel)\s+(\d+)", t):
        return Command("recurring_remove", {"index": int(m.group(1))})
    if re.match(r"^(every|each|monthly|weekly|daily|recurring add|repeat)\b", t):
        return Command("recurring_add", {"text": text.strip()})

    if m := re.fullmatch(r"export(?:\s+(csv|json|qif))?(?:\s+(.+))?", t):
        return Command(
            "export",
            {"format": m.group(1) or "csv", "period": _period(m.group(2) or "all") or "all_time"},
        )

    if m := re.fullmatch(r"(?:base|base currency|currency)\s+([a-z]{3})", t):
        return Command("set_currency", {"currency": m.group(1).upper()})
    if m := re.fullmatch(r"(?:tz|timezone|time zone)\s+(.+)", t):
        return Command(
            "set_timezone",
            {"tz": text.strip().split(maxsplit=1)[1] if " " in text.strip() else m.group(1)},
        )

    if t in {"digest", "weekly", "summary", "recap"}:
        return Command("digest")
    if t in {"monthly", "month recap", "monthly recap", "last month recap"}:
        return Command("digest", {"span": "month"})
    if t in {"review", "check", "unsure"}:
        return Command("review")
    if t in {"whoami", "me", "settings", "profile"}:
        return Command("settings")
    if t in {"owes", "who owes me", "debts", "loans", "lending"}:
        return Command("lending")
    if t in {"goals", "goal", "savings", "goal list"}:
        return Command("goals")
    if m := re.fullmatch(r"goal\s+(?:done|reached|close|remove)\s+(.+)", t):
        return Command("goal_done", {"name": m.group(1)})
    if m := re.fullmatch(
        r"goal\s+(.+?)\s+(\d+(?:[.,]\d{1,2})?)(?:\s+(?:by|before|until)\s+(.+))?", t
    ):
        return Command(
            "goal_add",
            {
                "name": _orig_tail(text, m.group(1)),
                "amount": m.group(2).replace(",", "."),
                "by": m.group(3),
            },
        )
    if m := re.fullmatch(
        r"(?:save|saved|put|add)\s+(\d+(?:[.,]\d{1,2})?)\s+(?:(?:to|for|into|in|towards)\s+)?(?:the\s+)?([a-z][\w -]{0,40}?)(?:\s+goal)?",
        t,
    ):
        return Command(
            "goal_save", {"amount": m.group(1).replace(",", "."), "name": m.group(2), "text": text}
        )
    if m := re.fullmatch(
        r"(?:withdraw|took out|take out)\s+(\d+(?:[.,]\d{1,2})?)\s+(?:from\s+)?(?:the\s+)?([a-z][\w -]{0,40}?)(?:\s+goal)?",
        t,
    ):
        return Command(
            "goal_save",
            {"amount": m.group(1).replace(",", "."), "name": m.group(2), "withdraw": True},
        )
    if re.match(r"^split\s+\S", t):
        return Command("split", {"text": text.strip()})
    if m := re.fullmatch(r"(?:person|profile|who is|whois)\s+([a-z][\w'-]{1,30})", t):
        return Command("person", {"name": m.group(1)})
    if m := re.fullmatch(r"history\s+(\d)", t):
        return Command("history", {"index": int(m.group(1))})
    return None


def _orig_tail(original: str, lowered_tail: str) -> str:
    """Recover original casing of a trailing argument."""
    idx = original.lower().rfind(lowered_tail)
    return original[idx : idx + len(lowered_tail)].strip() if idx >= 0 else lowered_tail.strip()
