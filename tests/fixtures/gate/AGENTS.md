# tests/fixtures/gate

Made-up project for the gate tests (`tests/test_gate_runner.py`, `test_gate_integrity.py`,
`test_gate_check_cli.py`, each via `FIXTURES = .../fixtures/gate`).

- `refunds_project/`: a tiny shop. `src/shop/refunds.py` has `refund(charge, amount)` and
  `fee(amount)` in integer cents; `laws/refunds.py` has three laws (`refund_is_capped`,
  `refund_never_negative`, and the example law `fee_example`); `laws/strategies.py` defines
  `st.cents()`; `lawkeep.toml` only sets `[check.pr] examples = 60` and a `[check.deep]` profile
  (no `[project] source`, so `src/` is found by default).
- `buggy_refunds.py`: `refunds.py` with a planted bug (amounts above 1000 cents are not capped).
  Tests copy it over the code, or compile it into stale bytecode for the `.pyc` side-door tests.
- `README.md`: the same in short.

Gotchas: tests copy `refunds_project/` to `tmp_path` and create `laws.lock` and the history with
`history.record_change(action="init")`; never run lawkeep on it in place. Add nothing inside
`refunds_project/`: a `conftest.py` would be loaded by this repository's own pytest run, a file
under `laws/` changes fingerprints, and an AGENTS.md changes what `lawkeep init` finds and
edits. Changing the laws or the cent ranges changes many expected results.

Test: `uv run pytest tests/test_gate_*.py`.
