# tests/fixtures/lifecycle

Made-up project and inputs for the law lifecycle tests (`draft`, `approve`, `propose`,
direction): `tests/test_lifecycle_flow.py`, `test_lifecycle_review_fixes.py`,
`test_lifecycle_units.py`, `test_lifecycle_draft_edits.py` and `test_gate_review_fixes.py` (each via `FIX = .../fixtures/lifecycle`).

- `teashop/`: a tea shop. `src/teashop/pricing.py` has `total(price, quantity)`,
  `discount(amount, percent)` and a private `_internal_helper`; `laws/pricing.py` has one law,
  `discount_never_raises_price`; `laws/strategies.py` defines `st.percents()`. It ships no
  `lawkeep.toml`, history or ledger: `make_project()` in the tests writes them (with
  `[agent]` pointing at `fake_agent.py` and `quick_check = false`) and records the `init`.
- `fake_agent.py`: stands in for a coding agent in `draft` (never calls any AI).
  `FAKE_AGENT_MODE` = `good` (a holding draft plus a new `st.cups()` generator), `failing`,
  `naughty` (also edits an approved law and an existing generator), `edit_looser` (loosens the
  approved law and explains it on a `Changed laws/pricing.py: ...` line), `edit_stricter`,
  `edit_settings` (appends `[permissions] add_law = "ask"` to `lawkeep.toml`), `edit_generator`
  (changes `percents()`), `delete_law` (each also writes the good draft and adds `cups()`),
  `edit_only` (no draft, only loosens the law), `nothing`, `crash`. It saves
  the prompt it got to `fake_agent_prompt.txt` in the project.
- `proposals/`: new versions of `discount_never_raises_price` for `propose`: `stricter.py` (adds
  `>= 0`), `looser.py` (`<= amount + 1`), `renamed.py` (import alias only: "same", auto-applied),
  `same.py` (same results but more than a rename: "unclear", asks), `skip.py` (`pytest.skip`),
  `added_law.py` (adds a second law), `import_error.py`, `broken_syntax.txt` (`.txt` so pytest
  and ruff never parse it).

Gotchas: never run lawkeep inside `teashop/` in place, and add nothing inside it (an AGENTS.md
there changes what `lawkeep init` finds and edits; a file under `laws/` changes fingerprints).
The expected direction of each proposal is asserted by name in the tests: if you change one,
check those tests.

Test: `uv run pytest tests/test_lifecycle_*.py tests/test_gate_review_fixes.py`.
