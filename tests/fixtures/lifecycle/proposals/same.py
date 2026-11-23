from teashop.pricing import discount

from lawkeep import given, law, st


@law("A discount never makes the price larger")
@given(amount=st.money(max_value=1000), percent=st.integers(min_value=0, max_value=100))
def discount_never_raises_price(amount, percent):
    result = discount(amount, percent)
    assert result <= amount
