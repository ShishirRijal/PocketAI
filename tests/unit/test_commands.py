from pocket.core.commands import is_no, is_yes, parse, pick_numbers


def test_basic_commands():
    assert parse("undo").name == "undo"
    assert parse("U").name == "undo"
    assert parse("/undo@PocketBot").name == "undo"
    assert parse("/week").args == {"period": "this_week"}
    assert parse("show last month").args == {"period": "last_month"}
    assert parse("help").name == "help"
    assert parse("23 eur lunch") is None


def test_edit_and_delete():
    assert parse("edit 2 amount 29").args == {"index": 2, "field": "amount", "value": "29"}
    assert parse("edit last amount to 29").args == {"index": 1, "field": "amount", "value": "29"}
    assert parse("e 3 note Dinner With Mom").args["value"] == "Dinner With Mom"
    assert parse("edit 1 12.50").args == {"index": 1, "field": "amount", "value": "12.50"}
    assert parse("delete 1, 3").args == {"indexes": [1, 3]}
    assert parse("d 2").args == {"indexes": [2]}
    assert parse("delete").args == {"for": "delete"}


def test_feature_commands():
    assert parse("budget cafes 80").args == {"category": "cafes", "amount": "80", "period": "month"}
    assert parse("budget groceries 60/week").args["period"] == "week"
    assert parse("every 15th 12.99 spotify").name == "recurring_add"
    assert parse("recurring stop 2").args == {"index": 2}
    assert parse("export qif last month").args == {"format": "qif", "period": "last_month"}
    assert parse("category rename Cafes to Coffee").args == {"old": "cafes", "new": "Coffee"}
    assert parse("person arjun").args == {"name": "arjun"}
    assert parse("split 60 dinner with arjun").name == "split"
    assert parse("tz Europe/Lisbon").args == {"tz": "Europe/Lisbon"}
    assert parse("currency npr").args == {"currency": "NPR"}


def test_answers():
    assert is_yes("Yes!") and is_yes("ok") and is_yes("hunchha")
    assert is_no("nope") and is_no("cancel") and is_no("hoina")
    assert pick_numbers("1 and 3", 3) == [1, 3]
    assert pick_numbers("4", 3) is None
    assert pick_numbers("1 coffee", 3) is None
