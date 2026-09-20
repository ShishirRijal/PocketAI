from pocket.core.normalize import normalize
from pocket.llm.rules.parser import extract


def test_normalize():
    assert normalize("  chiya  ३०  rs ") == "chiya 30 rs"
    assert normalize("lunch​ 12") == "lunch 12"
    assert normalize("“Rimi” 23") == '"Rimi" 23'
    assert normalize("coffee 4\n\nSent from my iPhone") == "coffee 4"
    assert normalize("ｃｏｆｆｅｅ　４") == "coffee 4"  # full-width forms (NFKC)
    assert normalize("taxi 12\n-- \nShishir") == "taxi 12"


def test_nepali_digits_parse(rules_ctx):
    [t] = extract(normalize("momo २५० रु"), rules_ctx).transactions
    assert t.amount == 250 and t.currency == "NPR"
