"""Import history from Pocket v1 (the Telegram bot: expenses/budgets/lendings in SQLite).

    pocket import-v1 /path/to/v1/pocket.db --currency NPR          # dry run
    pocket import-v1 /path/to/v1/pocket.db --currency NPR --apply

- expenses  -> transactions (category matched or created, raw text kept as a raw_message)
- budgets   -> monthly budgets
- lendings  -> `lent` per loan + `got_back` for the returned part, tagged with the person

v1 stored plain REAL amounts without a currency (its DEFAULT_CURRENCY, NPR by
default), so the currency is a flag. Idempotent: every imported row is keyed as
raw_messages(channel='v1', channel_msg_id='<table>:<id>'), so re-running skips
what's already there. The v1 database is opened read-only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

from pocket.core import money
from pocket.core.categorize import CatRef, match_category
from pocket.data.db import Database
from pocket.data.models import Budget, Transaction, User
from pocket.data.repositories import CategoryRepo, RawMessageRepo, TagRepo, TransactionRepo

V1_CATEGORY_ALIASES = {
    "food": "Restaurants",
    "other": "Miscellaneous",
    "bills": "Utilities",
    "grocery": "Groceries",
    "transportation": "Transport",
    "medical": "Health",
}


@dataclass
class ImportReport:
    expenses: int = 0
    budgets: int = 0
    lendings: int = 0
    skipped: int = 0
    new_categories: set[str] = field(default_factory=set)
    total_minor: dict[str, int] = field(default_factory=dict)

    def text(self, applied: bool) -> str:
        verb = "Imported" if applied else "Would import"
        totals = ", ".join(money.fmt(v, k) for k, v in self.total_minor.items()) or "nothing"
        lines = [
            f"{verb}: {self.expenses} expenses ({totals}), {self.budgets} budgets, {self.lendings} lending entries",
            f"Already imported / skipped: {self.skipped}",
        ]
        if self.new_categories:
            lines.append("New categories: " + ", ".join(sorted(self.new_categories)))
        if not applied:
            lines.append("Dry run. Re-run with --apply to write.")
        return "\n".join(lines)


def _parse_when(value: str | None, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    if not value:
        return datetime.now(UTC)
    value = value.strip()
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time(12, 0), tzinfo=zone).astimezone(
                UTC
            )
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # v1 wrote created_at with sqlite datetime('now'), which is UTC
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    exists = conn.execute(
        "select 1 from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return conn.execute(f"select * from {table} order by id").fetchall() if exists else []


def import_v1(
    db: Database, user_id: int, path: str | Path, currency: str, *, apply: bool = False
) -> ImportReport:
    currency = currency.upper()
    src = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    report = ImportReport()
    s = db.new_session()
    try:
        user = s.get(User, user_id)
        assert user is not None
        tz = user.timezone
        cats = CategoryRepo(s)
        raws = RawMessageRepo(s)
        txns = TransactionRepo(s)
        tags = TagRepo(s)
        # rates: only same-currency is exact; otherwise convert with the static table
        # at import time and say so in the note (historic ECB rates would be nicer)
        from pocket.services.fx import FxService

        rate: Decimal | None = None
        if currency != user.base_currency:
            table = FxService._static(user.base_currency)
            rate = (
                (Decimal(1) / table[currency]).quantize(Decimal("1e-10"))
                if currency in table
                else None
            )

        def base_amount(minor: int) -> int:
            return money.convert_minor(minor, currency, user.base_currency, rate) if rate else minor

        def category_for(name: str | None) -> int:
            wanted = V1_CATEGORY_ALIASES.get(
                (name or "other").strip().lower(), (name or "Miscellaneous").strip()
            )
            refs = [CatRef(c.id, c.name) for c in cats.active(user_id)]
            c, score = match_category(wanted, refs)
            if c and score >= 0.85:
                return c.id
            report.new_categories.add(wanted)
            return cats.create(user_id, wanted).id

        def claim(key: str, text: str | None, when: datetime) -> int | None:
            if raws.by_channel_id("v1", key):
                report.skipped += 1
                return None
            raw, _ = raws.insert_idempotent(
                user_id=user_id,
                channel="v1",
                channel_msg_id=key,
                text=text,
                media_json=None,
                received_at=when,
            )
            raws.mark(raw, "imported")
            return raw.id

        for r in _rows(src, "expenses"):
            when = _parse_when(r["date"], tz)
            raw_id = claim(f"expenses:{r['id']}", r["raw_message"], when)
            if raw_id is None:
                continue
            minor = money.to_minor(Decimal(str(r["amount"])), currency)
            note_bits = [r["note"]] if r["note"] else []
            try:
                items = json.loads(r["items"] or "[]") if "items" in r.keys() else []
                if items:
                    note_bits.append(
                        "items: "
                        + ", ".join(
                            str(i.get("name", i)) if isinstance(i, dict) else str(i) for i in items
                        )[:200]
                    )
            except (ValueError, TypeError):
                pass
            if rate:
                note_bits.append("fx: static estimate at import")
            tag_rows = (
                [tags.get_or_create(user_id, r["merchant"], "merchant")] if r["merchant"] else []
            )
            txns.add(
                Transaction(
                    user_id=user_id, amount_minor=minor, currency=currency, amount_base_minor=base_amount(minor),
                    fx_rate=rate, direction="expense", category_id=category_for(r["category"]),
                    merchant=r["merchant"], note="; ".join(note_bits) or None, occurred_at=when,
                    created_at=_parse_when(r["created_at"], tz), raw_message_id=raw_id,
                ),
                tag_rows,
            )  # fmt: skip
            report.expenses += 1
            report.total_minor[currency] = report.total_minor.get(currency, 0) + minor

        for r in _rows(src, "budgets"):
            if claim(f"budgets:{r['id']}", None, _parse_when(r["created_at"], tz)) is None:
                continue
            cat_id = category_for(r["category"])
            limit = base_amount(money.to_minor(Decimal(str(r["monthly_limit"])), currency))
            existing = s.scalars(
                select(Budget).where(
                    Budget.user_id == user_id,
                    Budget.category_id == cat_id,
                    Budget.period == "month",
                )
            ).first()
            if existing:
                existing.amount_minor = limit
            else:
                s.add(
                    Budget(user_id=user_id, category_id=cat_id, amount_minor=limit, period="month")
                )
            report.budgets += 1

        for r in _rows(src, "lendings"):
            when = _parse_when(r["created_at"], tz)
            raw_id = claim(f"lendings:{r['id']}", r["note"], when)
            if raw_id is None:
                continue
            person = tags.get_or_create(user_id, r["person_name"], "person")
            for direction, amount, at in (
                ("lent", r["amount"], when),
                (
                    "got_back",
                    r["amount_returned"],
                    _parse_when(r["settled_at"], tz) if r["settled_at"] else when,
                ),
            ):
                if not amount:
                    continue
                minor = money.to_minor(Decimal(str(amount)), currency)
                txns.add(
                    Transaction(
                        user_id=user_id, amount_minor=minor, currency=currency, amount_base_minor=base_amount(minor),
                        fx_rate=rate, direction=direction, occurred_at=at, created_at=at, note=r["note"],
                        raw_message_id=raw_id,
                    ),
                    [person],
                )  # fmt: skip
                report.lendings += 1

        if apply:
            s.commit()
        else:
            s.rollback()
        return report
    finally:
        s.close()
        src.close()
