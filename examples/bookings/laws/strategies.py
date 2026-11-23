"""Input generators for the booking laws (used as ``st.slots()`` and so on).

Times fall on one made-up Monday in 15-minute steps, so generated bookings often collide,
which is where the interesting cases are. This file is part of the laws and is fingerprinted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import strategies as hst

from bookings.calendar import Slot

OPENING = datetime(2030, 1, 7, 9, 0)  # Monday 09:00
STEP = timedelta(minutes=15)


def clock_times(max_steps: int = 40) -> hst.SearchStrategy[datetime]:
    """09:00 to 19:00 in 15-minute steps."""
    return hst.integers(min_value=0, max_value=max_steps).map(lambda n: OPENING + n * STEP)


def slots() -> hst.SearchStrategy[Slot]:
    """Slots of 15 minutes to 2 hours starting between 09:00 and 19:00."""
    return hst.builds(
        lambda start, steps: Slot(start, start + steps * STEP),
        clock_times(),
        hst.integers(min_value=1, max_value=8),
    )


def customers() -> hst.SearchStrategy[str]:
    return hst.sampled_from(["Ana", "Ben", "Chen", "Dee"])
