"""Discount codes for the made-up Corner Shop.

Codes:

- ``SAVE10``  10% off
- ``SAVE25``  25% off
- ``FIVEOFF`` $5.00 off (never below $0.00)

Unknown codes are ignored (the price stays the same). Codes are not case-sensitive.
"""

from __future__ import annotations

from decimal import Decimal

from billing.money import percent_of, round_money

PERCENT_CODES = {"SAVE10": Decimal(10), "SAVE25": Decimal(25)}
FIXED_CODES = {"FIVEOFF": Decimal("5.00")}
KNOWN_CODES = sorted([*PERCENT_CODES, *FIXED_CODES])


def apply_discount(price: Decimal, code: str | None) -> Decimal:
    """The price after the discount code, rounded to cents and never below zero."""
    price = round_money(price)
    if not code:
        return price
    code = code.strip().upper()
    if code in PERCENT_CODES:
        return price - percent_of(price, PERCENT_CODES[code])
    if code in FIXED_CODES:
        return max(Decimal("0.00"), price - FIXED_CODES[code])
    return price
