# tests/tamper

The cheating test suite: each known trick an agent could use to get past the gate, run against a
temporary git copy of `examples/billing`, usually on top of its planted bug. Each test shows the
trick is caught and reported. Where useful, it first shows the trick really works on plain pytest.
`README.md` lists the tricks in `test_tamper_tricks.py`. (Its last note, about gate steps ending in `xfail`
"not built yet", is out of date: `check` and `report` are built.)

- `test_tamper_tricks.py`: the tricks (law edits, conftest/sitecustomize/.pth/.pyc side doors,
  skip/xfail, fewer examples, lock and history rewrites, special-casing the failing input,
  routing around a law).
- `test_tamper_in_process.py`: tricks played by the code under test from inside the law run
  (it shares the laws' process); each must be defeated or flagged (result code 2), never a
  silent ALL LAWS HOLD.
- `test_tamper_examples.py`: the examples' lock and history are consistent, and their laws hold
  and fail as expected (also through the real gate).

Adding a trick: never weaken an assertion to make it pass. If lawkeep genuinely misses it, fix the
product, or mark it `xfail` with the reason and list it as not done in `AGENTS.md`.
Run: `uv run pytest tests/tamper -rxX` (45 tests, a few minutes). Never point a trick at
`examples/` itself: every test works on a temp copy, and the examples' `laws/` must not change.
