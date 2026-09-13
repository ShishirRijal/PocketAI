from datetime import date, timedelta

from sqlalchemy import select

from pocket.data.models import RecurringRule, Transaction
from pocket.services.recurring import next_date, parse_schedule, post_due_sync


async def test_budget_nudge(say):
    r = await say("budget cafes 20")
    assert "Budget set" in r and "€0.00 of €20.00" in r
    r = await say("coffee 10")
    assert "🟠" not in r
    r = await say("coffee 7")
    assert "🟠 Cafes: €17.00 of €20.00 this month" in r
    r = await say("coffee 5")
    assert "🔴 Cafes: €22.00 of €20.00" in r
    r = await say("budgets")
    assert "Cafes" in r and "110%" in r
    r = await say("budget remove cafes")
    assert "Removed 1" in r


async def test_weekly_budget(say):
    r = await say("budget groceries 50/week")
    assert "this week" in r


def test_parse_schedule():
    assert parse_schedule("every 15th 12.99 spotify") == ("monthly", 15, "12.99 spotify")
    assert parse_schedule("every month on the 1st rent 650") == ("monthly", 1, "rent 650")
    assert parse_schedule("weekly on monday 30 cleaning") == ("weekly", 0, "30 cleaning")
    assert parse_schedule("every friday 20 lunch with team") == ("weekly", 4, "20 lunch with team")
    assert parse_schedule("every day 3 coffee") == ("daily", None, "3 coffee")
    assert parse_schedule("yearly on 03-14 49 domain") == ("yearly", 314, "49 domain")
    assert parse_schedule("recurring add every 2nd 9.99 icloud") == ("monthly", 2, "9.99 icloud")
    assert parse_schedule("coffee 3") is None


def test_next_date():
    d = date(2026, 9, 28)  # monday
    assert next_date("monthly", 15, d, inclusive=True) == date(2026, 10, 15)
    assert next_date("monthly", 28, d, inclusive=True) == d
    assert next_date("monthly", 28, d, inclusive=False) == date(2026, 10, 28)
    assert next_date("monthly", 31, date(2026, 2, 1), inclusive=True) == date(2026, 2, 28)
    assert next_date("weekly", 0, d, inclusive=False) == d + timedelta(days=7)
    assert next_date("weekly", 4, d, inclusive=True) == date(2026, 10, 2)
    assert next_date("yearly", 314, d, inclusive=True) == date(2027, 3, 14)
    assert next_date("daily", None, d, inclusive=False) == date(2026, 9, 29)


async def test_recurring_flow(say, services, clock):
    r = await say("every 28th 12.99 spotify")
    assert "🔁 Recurring: €12.99 Spotify · every month on the 28th" in r
    assert "posts today" in r
    r = await say("recurring")
    assert "1. €12.99 Spotify" in r
    # the scheduler posts it at 09:00 local; our clock is 21:14 local, so it's due
    notes = post_due_sync(services.db, now=clock.now)
    assert len(notes) == 1 and "#spotify" in notes[0][1]
    assert post_due_sync(services.db, now=clock.now) == []  # not twice
    with services.db.session() as s:
        [t] = s.scalars(select(Transaction)).all()
        assert t.amount_minor == 1299 and t.category.name == "Subscriptions"
        rule = s.scalars(select(RecurringRule)).one()
        assert rule.next_run.date() == date(2026, 10, 28)
    r = await say("recurring stop 1")
    assert "Stopped" in r


async def test_recurring_catch_up(say, services, clock):
    await say("every day 3 coffee")
    clock.advance(days=3)
    notes = post_due_sync(services.db, now=clock.now)
    assert notes[0][1].count("€3.00") == 4


async def test_purge_deleted(say, services, settings):
    from datetime import timedelta
    from types import SimpleNamespace

    from pocket.data.db import utcnow
    from pocket.services.scheduler import purge_deleted

    await say("lunch 12")
    await say("delete 1")
    rt = SimpleNamespace(settings=settings, services=services)
    assert purge_deleted(rt) == 0  # disabled by default
    settings.purge_deleted_after_days = 30
    with services.db.session() as s:
        t = s.scalars(select(Transaction)).one()
        t.deleted_at = utcnow() - timedelta(days=31)
    assert purge_deleted(rt) == 1
    with services.db.session() as s:
        assert s.scalars(select(Transaction)).all() == []
