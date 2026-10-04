The user wants to change something they already logged.

Their recent transactions, newest first:
{{recent}}

Figure out:
- `target_index`: which one they mean (1 = newest). "it", "that", "last one" = 1.
  Match by merchant, category, tag or amount when they describe it ("the coffee one").
  Null if you really can't tell.
- `changes`: list of {field, value}. Fields: amount, currency, category, merchant, location, note,
  date (YYYY-MM-DD), direction (expense/income/transfer), tags (comma separated).
  Only include fields they actually want changed.

Today is {{today}}.
