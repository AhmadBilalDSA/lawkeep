"""Laws about appointment bookings (made-up Riverside Barbers)."""

from datetime import datetime

from bookings.calendar import BookingBook, BookingError, FixedClock, InThePast
from lawkeep import given, law, st


@law("Two bookings never overlap")
@given(now=st.clock_times(), requests=st.lists(st.slots(), max_size=8))
def bookings_never_overlap(now, requests):
    book = BookingBook(FixedClock(now))
    for slot in requests:
        try:
            book.book(slot, "Ana")
        except BookingError:
            pass  # refusing a booking is fine; accepting an overlapping one is not
    accepted = book.bookings()
    for i, first in enumerate(accepted):
        for second in accepted[i + 1 :]:
            assert not first.slot.overlaps(second.slot), (first, second)


@law("A free slot that starts now or later can always be booked")
@given(now=st.clock_times(), slot=st.slots())
def free_future_slot_can_be_booked(now, slot):
    # Companion of the overlap law: code that refuses every booking would pass that one.
    book = BookingBook(FixedClock(now))
    if slot.start < now:
        return
    booking = book.book(slot, "Ben")
    assert booking in book.bookings()


@law("Cancelling a booking frees its slot for someone else")
@given(slot=st.slots(), customer=st.customers())
def cancelling_frees_the_slot(slot, customer):
    book = BookingBook(FixedClock(datetime(2030, 1, 1)))  # before every generated slot
    first = book.book(slot, customer)
    book.cancel(first.id)
    assert book.is_free(slot)
    second = book.book(slot, "Chen")
    assert book.bookings() == [second]


@law("A booking never starts before the current time")
@given(now=st.clock_times(), slot=st.slots())
def bookings_are_never_in_the_past(now, slot):
    book = BookingBook(FixedClock(now))
    try:
        booking = book.book(slot, "Dee")
    except InThePast:
        assert slot.start < now
        return
    assert booking.slot.start >= now
