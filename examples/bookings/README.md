# Example: Riverside Barbers bookings

A made-up barber shop's appointment book for one chair. Customers book slots (start and end
time); "now" comes from a clock object, so laws can check "never in the past" at any time of day.

## The laws

| Law | In plain English |
| --- | --- |
| `booking_rules.bookings_never_overlap` | Two bookings never overlap |
| `booking_rules.free_future_slot_can_be_booked` | A free slot that starts now or later can always be booked |
| `booking_rules.cancelling_frees_the_slot` | Cancelling a booking frees its slot for someone else |
| `booking_rules.bookings_are_never_in_the_past` | A booking never starts before the current time |
| `slot_overlap.slots_overlap_exactly_when_they_share_time` | Two slots overlap exactly when they share some time; slots that only touch (one ends when the other starts) do not overlap |
| `free_slots.free_means_no_shared_time` | A slot is free exactly when it shares no time with any booking, and the list of bookings holds every accepted booking |

The second law is the companion of the first: code that refuses every booking would never
create an overlap, so "no overlaps" alone is too weak. The last two laws were added with
`lawkeep propose` (2026-11-22) to pin down the exact answers of `Slot.overlaps()` and
`BookingBook.is_free()` / `bookings()`; before them `lawkeep strength` flagged three laws as
one-sided (for example `is_free()` returning the slot itself, which counts as "yes", got
through). Now it reports no weak laws.

Input generators live in `laws/strategies.py`: times on one Monday from 09:00 to 19:00 in
15-minute steps and slots of 15 minutes to 2 hours. A small time window makes generated bookings
collide often, which is where the bugs are.

## See the gate catch a bug

```bash
cp -r examples/bookings /tmp/bookings-bug && cd /tmp/bookings-bug
git apply planted_bug.patch     # the overlap check only looks at the new slot's start
lawkeep check
```

Expected: `SOME LAWS FAIL` on "Two bookings never overlap", with a small failing input: first
09:15 to 09:30 is booked, then 09:00 to 09:30 is accepted although it covers the first booking.
