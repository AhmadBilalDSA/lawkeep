# tests/fixtures/strength

Made-up project for the strength and protection tests (`tests/test_strength_run.py`,
`test_protection_git.py`, `test_report_cli.py`, each via `FIXTURE = .../fixtures/strength/shop`).

- `shop/`: `src/shop/refunds.py` has `refund(charge, amount)`, `apply_fee(amount)` (2%, nothing
  under 1) and `loyalty_points(total)`. `laws/refunds.py` has `refund_is_capped` (one-sided on
  purpose: "refund always returns 0" also passes, so the quick check flags it); `laws/fees.py`
  has `fee_is_two_percent` (strong: catches every quick-check variant). No law calls
  `loyalty_points`, so it shows up as uncovered / not protected. `lawkeep.toml` sets only
  `source = ["src"]`.

Gotchas: tests copy `shop/` to `tmp_path` and often add files (`make_project(tmp_path, extra=...)`)
or a git history; never run lawkeep in place and add nothing inside `shop/` (one test snapshots
every file to prove strength writes nothing; an AGENTS.md there changes what `lawkeep init`
finds and edits). The weak and strong laws here are deliberate: do not "fix" them.

Test: `uv run pytest tests/test_strength_run.py tests/test_protection_git.py
tests/test_report_cli.py`.
