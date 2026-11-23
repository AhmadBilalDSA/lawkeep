"""Laws about rounding money (made-up Corner Shop)."""

from decimal import Decimal

from billing.money import round_money
from lawkeep import given, law, st


@law("Rounding money gives whole cents and is never off by more than half a cent")
@given(amount=st.decimals(min_value=-10_000, max_value=10_000, places=4))
def rounding_is_to_the_nearest_cent(amount):
    rounded = round_money(amount)
    assert rounded == rounded.quantize(Decimal("0.01"))
    assert abs(rounded - amount) <= Decimal("0.005")
