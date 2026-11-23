"""Made-up refund rules with a planted bug: refunds above 1000 cents are not capped."""


def refund(charge: int, amount: int) -> int:
    if amount < 0:
        raise ValueError("amount must not be negative")
    if amount > 1000:
        return amount
    return min(amount, charge)


def fee(amount: int) -> int:
    return amount * 2 // 100
