from pocket.core.normalize import normalize


def test_normalize():
    assert normalize("  chiya  ३०  rs ") == "chiya 30 rs"
    assert normalize("lunch​ 12") == "lunch 12"
    assert normalize("“Rimi” 23") == '"Rimi" 23'
    assert normalize("coffee 4\n\nSent from my iPhone") == "coffee 4"
    assert normalize("ｃｏｆｆｅｅ　４") == "coffee 4"  # full-width forms (NFKC)
    assert normalize("taxi 12\n-- \nShishir") == "taxi 12"


def test_nepali_digits():
    assert normalize("momo २५० रु") == "momo 250 रु"
