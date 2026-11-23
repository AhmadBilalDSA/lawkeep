# examples

Two made-up example projects (no private code), used by the docs, CI and the cheating test suite.

- `billing/` (Corner Shop): charges, card fees, refunds, discounts, rounding; 6 laws.
  `planted_bug.patch` makes a $0.25 refund return $0.26; `unprotected_change.patch` adds `refund_v2()`,
  which no law covers (the protection report marks it NOT protected).
- `bookings/` (Riverside Barbers): appointment slots with a settable clock; 6 laws; `planted_bug.patch`
  lets two bookings overlap.
- Each example and its `src/` folders have their own AGENTS.md; `laws/` folders deliberately do not.
- `make_example_history.py` wrote each example's first `laws.lock` and `laws/history.jsonl`
  through `history.record_change`. The companion laws added on 2026-11-22
  (`billing/laws/discount_amounts.py`, `bookings/laws/slot_overlap.py`, `bookings/laws/free_slots.py`)
  came in through real `lawkeep propose --root examples/<name>` runs, so their history entries
  are real ones (actor `agent:claude-code`, applied automatically as stricter). Rerunning the
  script rebuilds everything as approved drafts instead. Do not edit those files by hand: a
  hand-written lock is flagged. `lawkeep strength --root examples/<name>` reports no weak laws
  for either example.

Gotchas: do not add, edit or reformat files in `laws/` or `lawkeep.toml` (that changes their
fingerprints and shows up as unrecorded law changes; never put an AGENTS.md in `laws/`); work on
copies. Ruff does not lint the examples.
Check: `uv run lawkeep check --root examples/billing` (and `bookings`); `uv run pytest tests/tamper`.
