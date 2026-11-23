"""Refunds and fees for a made-up shop."""

from decimal import Decimal


def refund(charge: Decimal, amount: Decimal) -> Decimal:
    """Give back `amount` of `charge`, but never more than the charge itself."""
    if amount > charge:
        return charge
    return amount


def apply_fee(amount: Decimal) -> Decimal:
    """A 2% fee, nothing for amounts under 1."""
    if amount < Decimal("1"):
        return Decimal("0")
    return amount * Decimal("0.02")


def loyalty_points(total: int) -> int:
    """One point per 10 spent."""
    return total // 10
