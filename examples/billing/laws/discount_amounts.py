"""Laws about how much each discount code takes off (made-up Corner Shop)."""

from decimal import ROUND_HALF_UP, Decimal

from billing.discounts import apply_discount
from lawkeep import example, given, law, st

CENT = Decimal("0.01")


def cents(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


@law(
    "Each discount code takes off exactly what it promises: SAVE10 10%, SAVE25 25%, FIVEOFF "
    "$5.00 (never below $0.00), and no code or an unknown code takes off nothing",
    inputs={
        "price": "the price before the discount, in dollars",
        "code": "the discount code the customer typed (any letter case), or none",
    },
)
@given(price=st.money(0, 1000), code=st.discount_codes())
@example(price=Decimal("10.00"), code="SAVE10")
@example(price=Decimal("3.00"), code="FIVEOFF")
@example(price=Decimal("8.00"), code=None)
def discount_takes_off_exactly_what_it_promises(price, code):
    before = cents(price)
    name = (code or "").strip().upper()
    if name == "SAVE10":
        expected = before - cents(before * 10 / 100)
    elif name == "SAVE25":
        expected = before - cents(before * 25 / 100)
    elif name == "FIVEOFF":
        expected = max(Decimal("0.00"), before - Decimal("5.00"))
    else:
        expected = before
    assert apply_discount(price, code) == expected
