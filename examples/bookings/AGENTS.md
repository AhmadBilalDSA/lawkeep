# examples/bookings

Riverside Barbers bookings: a made-up lawkeep project (one chair's appointment book with a
settable clock). Used by the docs, CI and `tests/tamper/test_tamper_examples.py`. `README.md`
lists the 6 laws.

- `src/bookings/`: the code under test (see `src/AGENTS.md`); `lawkeep.toml` sets `source = ["src"]`.
- `laws/` (no AGENTS.md there on purpose): `booking_rules.py` (4 laws), `slot_overlap.py` and `free_slots.py` (exact-answer
  companions added with `lawkeep propose` on 2026-11-22), `strategies.py`
  (`st.clock_times()`, `st.slots()`, `st.customers()`: one Monday 09:00-19:00 in 15-minute steps,
  so bookings collide often), `history.jsonl`, `failing_examples.jsonl` (empty).
- `laws.lock`: written by `../make_example_history.py`, never by hand.
- `planted_bug.patch`: the overlap check only looks at the new slot's start, so two bookings can
  overlap.

Gotchas: never add, edit or reformat anything under `laws/` or `lawkeep.toml` (an unrecorded law
change; even an extra AGENTS.md there breaks the example). If you must, rerun
`uv run python examples/make_example_history.py examples/bookings`. The generator still uses a
lambda (known; see the root `AGENTS.md`). Apply the patch only in a copy.

Check: `uv run lawkeep check --root examples/bookings` (ALL LAWS HOLD, exit 0);
`uv run pytest tests/tamper/test_tamper_examples.py`.
