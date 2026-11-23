# tests/fixtures

Made-up projects and inputs used by the tests. Tests copy them to `tmp_path` first; never run
lawkeep on them in place. Each area has its own AGENTS.md describing its projects:

- `gate/`: `refunds_project/` plus `buggy_refunds.py`, for `test_gate_*`.
- `strength/`: `shop/` (one one-sided law, one strong law, one uncovered function), for
  `test_strength_*`, `test_protection_git.py`, `test_report_cli.py`.
- `lifecycle/`: `teashop/`, `fake_agent.py` (stands in for a coding agent in `draft`) and
  `proposals/` (new law versions: stricter, looser, rename, same-results refactor, skip, broken),
  for `test_lifecycle_*` and `test_gate_review_fixes.py`.

Gotchas: no fixture ships a `laws.lock` or history; tests create them with
`history.record_change(action="init")`. Do not add files (AGENTS.md, `conftest.py`, anything)
inside a fixture project (`<area>/<project>/`): files under its `laws/` change fingerprints, an
AGENTS.md changes what `lawkeep init` finds and edits (it adds its section to an existing one),
and a `conftest.py` would be loaded by this repository's own pytest run. Document projects in
the area's AGENTS.md instead. No private code.

Test: `uv run pytest tests/test_gate_*.py tests/test_strength_*.py tests/test_lifecycle_*.py`.
