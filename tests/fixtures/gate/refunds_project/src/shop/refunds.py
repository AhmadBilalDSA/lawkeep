"""Made-up refund rules for the gate tests."""


def refund(charge: int, amount: int) -> int:
    """Refund `amount` cents of a `charge` (in cents), never more than the charge."""
    if amount < 0:
        raise ValueError("amount must not be negative")
    return min(amount, charge)


def fee(amount: int) -> int:
    """A 2% fee, rounded down, in cents."""
    return amount * 2 // 100
