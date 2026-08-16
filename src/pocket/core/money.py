"""Money helpers. Everything in the DB is integer minor units."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# ISO-4217 minor unit exponents that aren't 2
_EXPONENTS = {
    "JPY": 0,
    "KRW": 0,
    "ISK": 0,
    "HUF": 2,
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}

SYMBOLS = {
    "EUR": "€",
    "USD": "$",
    "GBP": "£",
    "NPR": "₨",
    "INR": "₹",
    "JPY": "¥",
    "SEK": "kr ",
    "NOK": "kr ",
    "DKK": "kr ",
    "CHF": "CHF ",
    "PLN": "zł ",
    "CZK": "Kč ",
}


def exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def to_minor(amount: Decimal | float | int | str, currency: str) -> int:
    d = Decimal(str(amount))
    q = Decimal(1).scaleb(-exponent(currency))
    return int((d.quantize(q, rounding=ROUND_HALF_UP) * (10 ** exponent(currency))).to_integral())


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor).scaleb(-exponent(currency))


def fmt(minor: int, currency: str, *, sign: bool = False) -> str:
    cur = currency.upper()
    value = from_minor(abs(minor), cur)
    exp = exponent(cur)
    num = f"{value:,.{exp}f}"
    sym = SYMBOLS.get(cur)
    body = f"{sym}{num}" if sym else f"{num} {cur}"
    if minor < 0:
        return f"-{body}"
    if sign and minor > 0:
        return f"+{body}"
    return body


def convert_minor(minor: int, from_cur: str, to_cur: str, rate: Decimal) -> int:
    """rate = units of to_cur per 1 unit of from_cur."""
    value = from_minor(minor, from_cur) * rate
    return to_minor(value, to_cur)
