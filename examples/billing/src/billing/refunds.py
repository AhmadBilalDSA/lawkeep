"""Refunds at the made-up Corner Shop.

A refund gives the customer back part or all of a charge. The shop also gets back the matching
share of the card fee: refunding half the payment returns half the fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from billing.charges import Charge
from billing.money import round_money


class RefundTooLarge(ValueError):
    """The refund asked for more than the customer paid."""


@dataclass(frozen=True)
class Refund:
    charge_id: str
    total: Decimal  # what the customer gets back
    fee_returned: Decimal  # the share of the card fee the shop gets back


def refund(charge: Charge, amount: Decimal) -> Refund:
    """Refund ``amount`` of ``charge``. Refunding more than was paid raises RefundTooLarge."""
    amount = round_money(amount)
    if amount < 0:
        raise ValueError(f"a refund cannot be negative: {amount}")
    if amount > charge.total:
        raise RefundTooLarge(f"refund {amount} is more than the payment {charge.total}")
    if charge.total == 0:
        return Refund(charge_id=charge.id, total=amount, fee_returned=Decimal("0.00"))
    fee_returned = round_money(charge.card_fee * amount / charge.total)
    return Refund(charge_id=charge.id, total=amount, fee_returned=fee_returned)
