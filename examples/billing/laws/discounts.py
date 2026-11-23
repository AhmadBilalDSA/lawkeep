"""Laws about discount codes (made-up Corner Shop)."""

from billing.discounts import apply_discount
from billing.money import round_money
from lawkeep import given, law, st


@law("A discount code never raises the price and never takes it below zero")
@given(price=st.money(0, 1000), code=st.discount_codes())
def discount_stays_between_zero_and_price(price, code):
    assert 0 <= apply_discount(price, code) <= round_money(price)
