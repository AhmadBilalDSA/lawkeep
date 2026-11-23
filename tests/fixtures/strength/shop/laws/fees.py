from shop import refunds

from lawkeep import given, law, st


@law("The fee is 2% of the amount, or 0 under 1")
@given(amount=st.money())
def fee_is_two_percent(amount):
    expected = 0 if amount < 1 else amount * 2 / 100
    assert refunds.apply_fee(amount) == expected
