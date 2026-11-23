from shop.refunds import fee, refund

from lawkeep import given, law, st


@law("A refund never exceeds the original charge")
@given(charge=st.cents(), amount=st.cents())
def refund_is_capped(charge, amount):
    assert refund(charge, amount) <= charge


@law("A refund is never negative")
@given(charge=st.cents(), amount=st.cents())
def refund_never_negative(charge, amount):
    assert refund(charge, amount) >= 0


@law("The fee on a refund of 100 cents is 2 cents")
def fee_example():
    assert fee(100) == 2
