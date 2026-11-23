# examples/billing

Corner Shop billing: a made-up lawkeep project (card charges with a 2% fee, refunds that give
back a fair share of that fee, discount codes, rounding to cents). Used by the docs, CI, the
cheating test suite and some gate tests. `README.md` lists the 6 laws.

- `src/billing/`: the code under test (see `src/AGENTS.md`); `lawkeep.toml` sets `source = ["src"]`.
- `laws/` (no AGENTS.md there on purpose): `refunds.py` (3 laws), `discounts.py`, `rounding.py`,
  `discount_amounts.py` (the exact-amount companion of the discount law, added with `lawkeep propose`),
  `strategies.py` (`st.charges()`, `st.refund_requests()`, `st.discount_codes()`),
  `history.jsonl` (an `init`, one `add_law` per law; the last one from a real `propose`) and
  `failing_examples.jsonl` (empty).
  Every file there except the two `.jsonl` files is fingerprinted in `laws.lock`.
- `laws.lock`: written by `../make_example_history.py`, never by hand.
- `planted_bug.patch`: the refund also hands back the fee share (a $0.25 refund returns $0.26).
- `unprotected_change.patch`: adds `refund_v2()`, which no law tests; `lawkeep report` marks it
  NOT protected.

Gotchas: never add, edit or reformat anything under `laws/` or `lawkeep.toml` (an unrecorded law
change; even an extra AGENTS.md there breaks the example). If you must change them, rerun
`uv run python examples/make_example_history.py examples/billing`. Both patches target
`src/billing/refunds.py`, so editing that file can stop them applying. Apply patches only in a copy.

Check: `uv run lawkeep check --root examples/billing` (ALL LAWS HOLD, exit 0);
`uv run pytest tests/tamper`.
