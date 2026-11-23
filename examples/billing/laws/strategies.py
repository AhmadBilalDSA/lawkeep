"""Input generators for the billing laws (used as ``st.charges()`` and so on).

This file is part of the laws: lawkeep fingerprints it, so changing it outside lawkeep (for
example to generate only easy inputs) is flagged as an unrecorded law change.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import strategies as hst

from billing.charges import Charge, charge
from billing.discounts import KNOWN_CODES
from lawkeep.strategies import money


def discount_codes() -> hst.SearchStrategy[str | None]:
    """No code, every real code (in any letter case) and a made-up code nobody sells."""
    real = hst.sampled_from(KNOWN_CODES)
    return hst.one_of(
        hst.none(),
        real,
        real.map(str.lower),
        hst.just("NOT-A-CODE"),
    )


def charges() -> hst.SearchStrategy[Charge]:
    """Charges made by the real ``charge()`` function: $0.00 to $1,000.00, any discount code."""
    return hst.builds(charge, price=money(0, 1000), discount_code=discount_codes())


@hst.composite
def refund_requests(draw) -> tuple[Charge, Decimal]:
    """A charge and a refund amount from $0.00 up to the full payment."""
    c = draw(charges())
    amount = draw(money(0, c.total))
    return c, amount
