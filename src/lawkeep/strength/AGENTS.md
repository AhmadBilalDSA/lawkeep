# src/lawkeep/strength

Law strength: the quick check (deliberately broken versions of functions), planted bugs, vacuity, weak test data and
gaps. Also powers the protection report (`protection.py`) and `direction`'s planted-bug evidence.

- `core.py`: `run_strength`, `run_quick_check`, `catch_matrix` (public API, re-exported with
  `result_for` from `__init__.py`).
- `_worker.py` is the `strength` mode of the law-running core (`lawkeep/lawrun.py`): one
  isolated pytest process (same start-up, environment, request and results pipe as the gate)
  with coverage contexts per law and in-memory swaps; `core.run_worker` starts it and
  `core.worker_problem` explains an unfinished run. Records stream over the pipe, so a run
  stopped by its time limit keeps what it found. The user's files are never written.
- `mutants.py`: lawkeep's own small ast mutator (operators modelled on mutmut's). Using it instead of
  mutmut is pending maintainer review (see `docs/PLAN.md` → Decisions).
- `sourcemap.py` maps functions to files without running code; `render.py` gives the human screens
  (Rich, via `lawkeep.ui`; status words "Protected / Partly protected / Not protected — read this one")
  and the Markdown for pull-request comments (no Rich);
  `types.py` holds `PlantedBug` and `CatchMatrix`.

The quick check only swaps functions the law calls by name; the law's own inputs and local
variables are not names of functions (`_worker.law_source_names`). A one-sided law is not
flagged when another approved law in the same run catches every broken version it lets through
(`core._credit_companions`; drafts never count).

Gotcha: when two target files define the same law id (an approved law and its draft), result keys are
`"<law_id>@<file>"`; use `strength.result_for`. The per-run time limit uses SIGALRM (not on Windows).

Tests: `tests/test_strength_*.py`, `tests/test_protection_git.py`, `tests/test_report_cli.py`.
