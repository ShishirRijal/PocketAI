"""Realistic fake history for demos, screenshots and dashboard development.

`pocket demo --months 6` fills a (preferably empty) database with a plausible
Tallinn-life spending pattern: rent, groceries a few times a week, coffee most
mornings, transport, subscriptions, the odd trip to Nepal in NPR, salary.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from pocket.core import money
from pocket.data.db import Database
from pocket.data.models import Budget, Goal, RecurringRule, Transaction
from pocket.data.repositories import CategoryRepo, TagRepo, TransactionRepo, UserRepo
from pocket.services.recurring import next_date

NPR_PER_EUR = Decimal("174.4")

# (category, merchant, low, high, tags, per-week probability or None)
DAILY = [
    ("Cafes", "Rimi Kohvik", 2.8, 4.9, ["coffee"], 0.55),
    ("Cafes", "Kohvik Rost", 3.5, 6.5, ["coffee", "pastry"], 0.18),
    ("Transport", "Bolt", 4.5, 14.0, ["taxi"], 0.12),
    ("Restaurants", "Wolt", 11.0, 24.0, ["takeaway"], 0.14),
    ("Restaurants", "Vapiano", 12.0, 21.0, ["lunch"], 0.10),
]
WEEKLY = [
    ("Groceries", "Rimi", 18.0, 62.0, ["groceries"], 2.1),
    ("Groceries", "Selver", 12.0, 45.0, ["groceries"], 1.0),
    ("Groceries", "Lidl", 9.0, 30.0, ["groceries"], 0.6),
    ("Entertainment", "Apollo Kino", 9.0, 16.0, ["movie"], 0.2),
    ("Shopping", "Zara", 25.0, 80.0, ["clothes"], 0.12),
    ("Health", "Apotheka", 5.0, 28.0, ["pharmacy"], 0.25),
    ("Restaurants", "Pub Hell Hunt", 14.0, 38.0, ["drinks", "friends"], 0.35),
]
MONTHLY = [
    (1, "Rent", "Landlord", 650.0, ["rent"]),
    (3, "Utilities", "Elektrum", 35.0, ["electricity"]),
    (5, "Utilities", "Telia", 19.99, ["internet"]),
    (15, "Subscriptions", "Spotify", 11.99, ["spotify"]),
    (18, "Subscriptions", "iCloud", 2.99, ["icloud"]),
    (20, "Transport", "Ühiskaart", 30.0, ["public-transport"]),
    (22, "Subscriptions", "ChatGPT", 23.0, ["ai"]),
]
PEOPLE = ["arjun", "sita", "maria", "kristjan"]


def seed_demo(db: Database, months: int = 6, seed: int = 42, user_id: int | None = None) -> int:
    rng = random.Random(seed)
    with db.session() as s:
        users = UserRepo(s).all()
        user = (
            UserRepo(s).get(user_id)
            if user_id
            else (users[0] if users else UserRepo(s).create(name="demo"))
        )
        assert user is not None
        tz = ZoneInfo(user.timezone)
        cats = CategoryRepo(s)
        tags = TagRepo(s)
        txns = TransactionRepo(s)
        today = datetime.now(tz).date()
        start = (today.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
        n = 0

        def add(d: date, cat: str, merchant: str | None, amount: float, tag_names: list[str], *,
                currency: str = "EUR", direction: str = "expense", hour: int | None = None,
                note: str | None = None, conf: float | None = None) -> None:  # fmt: skip
            nonlocal n
            h = hour if hour is not None else rng.choice([8, 9, 12, 13, 17, 18, 19, 20])
            when = datetime.combine(d, time(h, rng.randint(0, 59)), tzinfo=tz).astimezone(UTC)
            if when > datetime.now(UTC):
                return
            minor = money.to_minor(Decimal(str(round(amount, 2))), currency)
            rate = None
            base_minor = minor
            if currency == "NPR":
                rate = (Decimal(1) / NPR_PER_EUR).quantize(Decimal("1e-10"))
                base_minor = money.convert_minor(minor, "NPR", "EUR", rate)
            c = cats.by_name(user.id, cat) or cats.create(user.id, cat)
            tag_rows = [
                tags.get_or_create(user.id, t, "person" if t in PEOPLE else None) for t in tag_names
            ]
            if merchant:
                tag_rows.append(tags.get_or_create(user.id, merchant, "merchant"))
            txns.add(
                Transaction(
                    user_id=user.id, amount_minor=minor, currency=currency, amount_base_minor=base_minor,
                    fx_rate=rate, direction=direction, category_id=c.id, merchant=merchant, note=note,
                    occurred_at=when, created_at=when,
                    llm_confidence=Decimal(str(conf if conf is not None else round(rng.uniform(0.86, 0.99), 2))),
                ),
                tag_rows,
            )  # fmt: skip
            n += 1

        # a two-week trip to Kathmandu in the middle of the range
        trip_start = start + timedelta(days=int(31 * months * 0.55))
        trip = {trip_start + timedelta(days=i) for i in range(14)}

        d = start
        while d <= today:
            if d in trip:
                add(
                    d,
                    "Cafes",
                    None,
                    rng.choice([30, 40, 50, 60]),
                    ["chiya"],
                    currency="NPR",
                    hour=8,
                )
                add(
                    d,
                    "Restaurants",
                    None,
                    rng.uniform(250, 900),
                    ["momo", "dal-bhat"],
                    currency="NPR",
                    hour=13,
                )
                if rng.random() < 0.5:
                    add(d, "Transport", "Pathao", rng.uniform(150, 450), ["ride"], currency="NPR")
                if rng.random() < 0.2:
                    add(
                        d,
                        "Gifts",
                        None,
                        rng.uniform(1500, 5000),
                        ["family"],
                        currency="NPR",
                        note="for family",
                    )
            else:
                for cat, mer, lo, hi, tg, p in DAILY:
                    weekend = d.weekday() >= 5
                    if rng.random() < (p * (0.6 if weekend and cat == "Cafes" else 1.0)):
                        extra = [rng.choice(PEOPLE)] if rng.random() < 0.2 else []
                        add(d, cat, mer, rng.uniform(lo, hi), tg + extra)
                for cat, mer, lo, hi, tg, per_week in WEEKLY:
                    if rng.random() < per_week / 7:
                        add(d, cat, mer, rng.uniform(lo, hi), tg)
            for day, cat, mer, amt, tg in MONTHLY:
                if d.day == day:
                    add(d, cat, mer, amt, tg, hour=9, conf=0.99)
            if d.day == 25:
                add(d, "Salary", "Employer", rng.choice([3150, 3150, 3150, 3400]), ["salary"],
                    direction="income", hour=10, conf=0.99)  # fmt: skip
            if d == trip_start - timedelta(days=1):
                add(d, "Travel", "Qatar Airways", 780.0, ["flight", "nepal"], hour=21)
            if rng.random() < 0.01:
                add(
                    d,
                    "Miscellaneous",
                    None,
                    rng.uniform(5, 60),
                    ["misc"],
                    conf=0.55,
                    note="not sure what this was",
                )
            d += timedelta(days=1)

        # a couple of loans between friends
        add(
            today - timedelta(days=9),
            "Miscellaneous",
            None,
            40.0,
            ["arjun"],
            direction="lent",
            conf=0.95,
        )
        add(
            today - timedelta(days=3),
            "Miscellaneous",
            None,
            15.0,
            ["arjun"],
            direction="got_back",
            conf=0.95,
        )
        add(
            today - timedelta(days=6),
            "Miscellaneous",
            None,
            25.0,
            ["maria"],
            direction="borrowed",
            conf=0.95,
        )
        for tname in ("arjun", "maria"):
            tags.get_or_create(user.id, tname, "person")

        # budgets and the recurring rules behind the monthly items
        for cat, limit in (
            ("Cafes", 80),
            ("Restaurants", 150),
            ("Groceries", 420),
            ("Shopping", 60),
        ):
            c = cats.by_name(user.id, cat)
            if (
                c
                and not s.scalars(
                    select(Budget).where(Budget.user_id == user.id, Budget.category_id == c.id)
                ).first()
            ):
                s.add(
                    Budget(
                        user_id=user.id, category_id=c.id, amount_minor=limit * 100, period="month"
                    )
                )
        for day, cat, mer, amt, tg in MONTHLY:
            c = cats.by_name(user.id, cat)
            nxt = next_date("monthly", day, today, inclusive=False)
            s.add(RecurringRule(
                user_id=user.id, amount_minor=money.to_minor(Decimal(str(amt)), "EUR"), currency="EUR",
                category_id=c.id if c else None, merchant=mer, tags=tg, cadence="monthly", anchor=day,
                next_run=datetime.combine(nxt, time(9, 0), tzinfo=tz).astimezone(UTC),
            ))  # fmt: skip

        # a savings goal with a few monthly contributions
        if not s.scalars(
            select(Goal).where(Goal.user_id == user.id, Goal.slug == "japan-trip")
        ).first():
            due = datetime.combine(date(today.year + 1, 6, 30), time(12), tzinfo=tz)
            s.add(
                Goal(
                    user_id=user.id,
                    name="Japan trip",
                    slug="japan-trip",
                    target_minor=250000,
                    due=due,
                )
            )
            goal_tag = tags.get_or_create(user.id, "goal-japan-trip")
            for back in (90, 60, 30, 2):
                when = datetime.combine(
                    today - timedelta(days=back), time(10), tzinfo=tz
                ).astimezone(UTC)
                txns.add(
                    Transaction(
                        user_id=user.id, amount_minor=20000, currency="EUR", amount_base_minor=20000,
                        direction="transfer", note="saved for Japan trip", occurred_at=when, created_at=when,
                    ),
                    [goal_tag],
                )  # fmt: skip
        return n
