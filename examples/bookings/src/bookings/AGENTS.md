# examples/bookings/src/bookings

The made-up Riverside Barbers appointment book that the example laws check.

- `calendar.py`: `Slot`, `Booking`, `BookingBook` (book, cancel), `FixedClock` ("now" is
  injected so laws can test "never in the past" at any time), and the errors `BookingError`,
  `SlotTaken`, `InThePast`, `InvalidSlot`. Target of `../../planted_bug.patch`.

Gotchas: `laws/booking_rules.py` and `laws/strategies.py` import these names; renaming one breaks
the laws, which cannot be changed without rebuilding the history. Keep it made-up code.

Check: `uv run lawkeep check --root examples/bookings`.
