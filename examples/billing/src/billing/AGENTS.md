# examples/billing/src/billing

The made-up Corner Shop's billing code that the example laws check. Amounts are `Decimal` dollars.

- `money.py`: `round_money` (half up to whole cents), `percent_of`.
- `charges.py`: `Charge`, `charge()` (the card processor keeps a 2% fee).
- `refunds.py`: `Refund`, `RefundTooLarge`, `refund()` (the shop gets back the matching share of
  the fee). Target of `../../planted_bug.patch` and `../../unprotected_change.patch`.
- `discounts.py`: `apply_discount()` with `SAVE10`, `SAVE25`, `FIVEOFF` (`KNOWN_CODES`, used by
  `laws/strategies.py`).

Gotchas: the laws and `laws/strategies.py` import these names; renaming one breaks the laws, and
the laws cannot be changed without rebuilding the history. Changing behaviour should make a law
fail: that is the demo. Keep it made-up code (no private code).

Check: `uv run lawkeep check --root examples/billing`; `uv run lawkeep strength --root examples/billing`.
