"""Made-up tea shop pricing rules (test fixture for lawkeep; not real code)."""

from decimal import Decimal


def total(price: Decimal, quantity: int) -> Decimal:
    """Price of `quantity` cups at `price` each. Never negative."""
    return price * quantity


def discount(amount: Decimal, percent: int) -> Decimal:
    """The amount after a percentage discount (percent is 0 to 100)."""
    return amount - amount * percent / 100


def _internal_helper(x):
    return x * 42
