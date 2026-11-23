from shop.refunds import refund

from lawkeep import given, law, st


@law("A refund never exceeds the original charge")
@given(charge=st.money(), amount=st.money())
def refund_is_capped(charge, amount):
    assert refund(charge, amount) <= charge
