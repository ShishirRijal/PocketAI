from datetime import UTC, date, datetime
from decimal import Decimal

from pocket.core import money
from pocket.core.categorize import CatRef, keyword_category, match_category
from pocket.core.dates import find_date, period_range, resolve_occurred_at

NOW = datetime(2026, 9, 28, 18, 14, tzinfo=UTC)  # 21:14 in Tallinn
TODAY = date(2026, 9, 28)  # monday


def test_minor_units_roundtrip():
    assert money.to_minor("29", "EUR") == 2900
    assert money.to_minor(6.5, "EUR") == 650
    assert money.to_minor(0.105, "EUR") == 11  # half up
    assert money.to_minor(500, "JPY") == 500
    assert money.from_minor(2900, "EUR") == Decimal("29.00")


def test_fmt():
    assert money.fmt(2900, "EUR") == "€29.00"
    assert money.fmt(3000, "NPR") == "₨30.00"
    assert money.fmt(123456, "USD") == "$1,234.56"
    assert money.fmt(500, "XYZ") == "5.00 XYZ"
    assert money.fmt(-250, "EUR") == "-€2.50"


def test_convert():
    assert money.convert_minor(3000, "NPR", "EUR", Decimal("0.00675")) == 20


def test_find_date():
    assert find_date("lunch yesterday", TODAY)[0] == date(2026, 9, 27)
    assert find_date("chiya aja", TODAY)[0] == TODAY
    assert find_date("3 days ago taxi", TODAY)[0] == date(2026, 9, 25)
    assert find_date("on friday", TODAY)[0] == date(2026, 9, 25)
    assert find_date("last monday", TODAY)[0] == date(2026, 9, 21)
    assert find_date("15 sep dinner", TODAY)[0] == date(2026, 9, 15)
    assert find_date("on the 3rd", TODAY)[0] == date(2026, 9, 3)
    assert find_date("on the 30th", TODAY)[0] == date(2026, 8, 30)
    assert find_date("12.5 sandwich", TODAY)[0] is None


def test_resolve_occurred_at():
    assert resolve_occurred_at(None, "Europe/Tallinn", NOW) == NOW
    assert resolve_occurred_at("2026-09-28", "Europe/Tallinn", NOW) == NOW
    y = resolve_occurred_at("2026-09-27", "Europe/Tallinn", NOW)
    assert y == datetime(2026, 9, 27, 9, 0, tzinfo=UTC)  # noon tallinn
    # future gets clamped
    assert resolve_occurred_at("2026-12-01", "Europe/Tallinn", NOW) == NOW
    assert resolve_occurred_at("garbage", "Europe/Tallinn", NOW) == NOW


def test_period_range():
    r = period_range("this_month", "Europe/Tallinn", NOW)
    assert r.start == datetime(2026, 8, 31, 21, 0, tzinfo=UTC)
    assert r.label == "September" and r.so_far
    r = period_range("last_week", "Europe/Tallinn", NOW)
    assert r.start.date() == date(2026, 9, 20) and r.end.date() == date(2026, 9, 27)
    r = period_range("custom", "Europe/Tallinn", NOW, "2026-09-01", "2026-09-30")
    assert r.end == datetime(2026, 9, 30, 21, 0, tzinfo=UTC)


def test_match_category():
    cats = [CatRef(1, "Groceries"), CatRef(2, "Cafes"), CatRef(3, "Food/Takeaway")]
    assert match_category("grocery", cats)[0].id == 1
    assert match_category("GROCERIES", cats)[0].id == 1
    assert match_category("cafe", cats)[0].id == 2
    assert match_category("coffee", cats)[0].id == 2
    assert match_category("takeaway", cats)[0].id == 3
    assert match_category("Grocceries", cats)[0].id == 1
    assert match_category("passport", cats)[0] is None


def test_keyword_category_longest_wins():
    assert keyword_category("bolt food order") == "Restaurants"
    assert keyword_category("bolt ride") == "Transport"
    assert keyword_category("2 coffees") == "Cafes"
