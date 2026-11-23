"""Laws about which slots are free (made-up Riverside Barbers)."""

from datetime import datetime

from bookings.calendar import BookingBook, BookingError, FixedClock
from lawkeep import given, law, st


@law(
    "A slot is free exactly when it shares no time with any booking, and the list of "
    "bookings holds every accepted booking",
    inputs={
        "booked": "slots customers asked for, in order (some may be refused)",
        "slot": "the slot we ask about",
    },
)
@given(booked=st.lists(st.slots(), max_size=6), slot=st.slots())
def free_means_no_shared_time(booked, slot):
    book = BookingBook(FixedClock(datetime(2030, 1, 1)))  # before every generated slot
    accepted = []
    for s in booked:
        try:
            accepted.append(book.book(s, "Ana"))
        except BookingError:
            pass
    assert sorted(book.bookings(), key=lambda b: b.id) == sorted(accepted, key=lambda b: b.id)
    shares_time = any(
        max(slot.start, b.slot.start) < min(slot.end, b.slot.end) for b in accepted
    )
    assert book.is_free(slot) is (not shares_time)
