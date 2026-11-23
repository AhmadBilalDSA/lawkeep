"""Money helpers for the made-up Corner Shop billing module.

All amounts are ``Decimal`` values in dollars. Rounding is always to whole cents, half up
(``$0.125`` becomes ``$0.13``), the way the shop's card processor rounds.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def round_money(amount: Decimal) -> Decimal:
    """Round to whole cents, half up: ``Decimal("2.345")`` -> ``Decimal("2.35")``."""
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def percent_of(amount: Decimal, percent: Decimal) -> Decimal:
    """``percent`` percent of ``amount``, rounded to cents: 2% of $10.00 -> $0.20."""
    return round_money(Decimal(amount) * Decimal(percent) / Decimal(100))
