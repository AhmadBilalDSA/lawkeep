"""Charging a customer's card at the made-up Corner Shop.

The card processor keeps a fee of 2% of the charged total (rounded to cents). When the shop
refunds a customer, it gets back the same share of that fee (see ``billing.refunds``).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal

from billing.discounts import apply_discount
from billing.money import percent_of, round_money

CARD_FEE_PERCENT = Decimal(2)

_ids = itertools.count(1)


@dataclass(frozen=True)
class Charge:
    """One card payment. ``total`` is what the customer paid."""

    id: str
    total: Decimal
    card_fee: Decimal


def charge(price: Decimal, discount_code: str | None = None) -> Charge:
    """Charge ``price`` (after an optional discount code) to the customer's card."""
    if Decimal(price) < 0:
        raise ValueError(f"cannot charge a negative price: {price}")
    total = apply_discount(round_money(price), discount_code)
    return Charge(id=f"ch-{next(_ids)}", total=total, card_fee=percent_of(total, CARD_FEE_PERCENT))
