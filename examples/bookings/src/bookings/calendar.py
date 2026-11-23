"""Appointment bookings for the made-up Riverside Barbers.

A customer books a slot (start and end time). Two bookings may never overlap, a cancelled
booking frees its slot, and nobody can book a slot that starts before "now". "Now" comes from a
clock object, so the rules can be checked at any time of day without waiting.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime


class BookingError(Exception):
    """Base class for booking problems."""


class SlotTaken(BookingError):
    """The slot overlaps an existing booking."""


class InThePast(BookingError):
    """The slot starts before the current time."""


class InvalidSlot(BookingError, ValueError):
    """The slot ends before (or when) it starts."""


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime

    def overlaps(self, other: Slot) -> bool:
        """True when the two slots share any time (touching ends do not count)."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Booking:
    id: str
    slot: Slot
    customer: str


class FixedClock:
    """A clock that always says the same time (handy for laws and tests)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class BookingBook:
    """All bookings for one chair."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self._bookings: dict[str, Booking] = {}
        self._ids = itertools.count(1)

    def bookings(self) -> list[Booking]:
        return sorted(self._bookings.values(), key=lambda b: (b.slot.start, b.id))

    def is_free(self, slot: Slot) -> bool:
        return not any(slot.overlaps(b.slot) for b in self._bookings.values())

    def book(self, slot: Slot, customer: str) -> Booking:
        if slot.end <= slot.start:
            raise InvalidSlot(f"a slot must end after it starts: {slot}")
        if slot.start < self._clock.now():
            raise InThePast(f"{slot.start} is before now ({self._clock.now()})")
        if not self.is_free(slot):
            raise SlotTaken(f"{slot.start}-{slot.end} overlaps an existing booking")
        booking = Booking(id=f"b-{next(self._ids)}", slot=slot, customer=customer)
        self._bookings[booking.id] = booking
        return booking

    def cancel(self, booking_id: str) -> None:
        """Cancel a booking; its slot becomes free again. Unknown ids raise KeyError."""
        del self._bookings[booking_id]
