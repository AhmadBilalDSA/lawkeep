from teashop.pricing import discount

from lawkeep import given, law, st


@law("A discount never makes the price larger")
@given(amount=st.money(max_value=1000), percent=st.integers(min_value=0, max_value=100))
def discount_never_raises_price(amount, percent):
    assert discount(amount, percent) <= amount


@law("A discount of zero percent changes nothing")
@given(amount=st.money(max_value=1000))
def zero_discount_is_identity(amount):
    assert discount(amount, 0) == amount
