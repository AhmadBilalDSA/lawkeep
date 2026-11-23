"""Laws about when two slots overlap (made-up Riverside Barbers)."""

from datetime import datetime

from bookings.calendar import Slot
from lawkeep import example, given, law, st


@law(
    "Two slots overlap exactly when they share some time; slots that only touch (one ends "
    "when the other starts) do not overlap",
    inputs={"first": "one appointment slot", "second": "another appointment slot"},
)
@given(first=st.slots(), second=st.slots())
@example(
    first=Slot(datetime(2030, 1, 7, 9, 0), datetime(2030, 1, 7, 10, 0)),
    second=Slot(datetime(2030, 1, 7, 10, 0), datetime(2030, 1, 7, 11, 0)),
)
@example(
    first=Slot(datetime(2030, 1, 7, 9, 0), datetime(2030, 1, 7, 10, 0)),
    second=Slot(datetime(2030, 1, 7, 9, 45), datetime(2030, 1, 7, 10, 30)),
)
def slots_overlap_exactly_when_they_share_time(first, second):
    shares_time = max(first.start, second.start) < min(first.end, second.end)
    assert first.overlaps(second) is shares_time
    assert second.overlaps(first) is shares_time
