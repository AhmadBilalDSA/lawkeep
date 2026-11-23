# docs/guide

User guides: `non-technical.md` (a week in the life, talking only to a coding agent), `developer.md`
(the CLI), `recipes.md` (common laws), `README.md` (index).

Keep command names, flags and result codes in line with `docs/ARCHITECTURE.md` section 7 and
`lawkeep <cmd> --help`. The README's `check` output was captured from a real run (2026-11-22, after the Rich/wording
redesign); the boxes in `non-technical.md` follow the same layout but are illustrative. The
"Overnight" story in `non-technical.md` and "Long runs without a person" in `developer.md`
describe park-and-keep-going and the one waiting list (2026-11-23). Keep
quoted output in line with `docs/OUTPUT-STYLE.md`.
`README.md` still says v0.1 "is still being built"; update it when the guides are refreshed.

Check: no tests read these files; try the commands they show on a copy of `examples/billing`.
