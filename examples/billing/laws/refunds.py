"""Laws about refunds (made-up Corner Shop)."""

from billing.refunds import RefundTooLarge, refund
from lawkeep import given, law, st


@law("A refund never exceeds the original payment")
@given(charge=st.charges(), amount=st.money(0, 1000))
def refund_never_exceeds_payment(charge, amount):
    try:
        result = refund(charge, amount)
    except RefundTooLarge:
        return  # refusing a refund that is too large is fine
    assert result.total <= charge.total


@law("Refunding exactly what was paid gives back exactly the payment and the whole card fee")
@given(charge=st.charges())
def full_refund_gives_back_exact_payment(charge):
    # Companion of the law above: without it, code that refuses every refund would pass.
    result = refund(charge, charge.total)
    assert result.total == charge.total
    assert result.fee_returned == charge.card_fee


@law("A partial refund gives back exactly the amount asked for and a fair share of the card fee")
@given(request=st.refund_requests())
def partial_refund_is_exact_and_fee_share_is_fair(request):
    charge, amount = request
    result = refund(charge, amount)
    assert result.total == amount
    assert 0 <= result.fee_returned <= charge.card_fee
