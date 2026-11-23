# Developer guide

lawkeep v0.1 is being built; this guide follows the v0.1 contract
([docs/ARCHITECTURE.md](../ARCHITECTURE.md)) and may change before release.

## Install

```bash
uv add --dev lawkeep        # once published; extras: lawkeep[deep] (CrossHair, L3),
                            # lawkeep[mcp] (MCP server), lawkeep[mutation] (mutmut)
lawkeep init --agent claude-code
```

`init` writes `lawkeep.toml`, creates `laws/` (with `laws/strategies.py`, an empty
`laws/history.jsonl` and `laws/failing_examples.jsonl`), records the first history entry and
`laws.lock`, adds `.lawkeep/` to `.gitignore`, and installs the adapter for your agent.

## Files on disk

```
lawkeep.toml                  settings + permissions               fingerprinted
laws.lock                     fingerprints, written only by lawkeep
laws/*.py                     approved laws                        fingerprinted
laws/strategies.py            your input generators (st.charges()) fingerprinted
laws/conftest.py              law fixtures (optional)              fingerprinted
laws/history.jsonl            every law change, append-only
laws/failing_examples.jsonl   every failing input found, append-only, replayed on every run
laws/drafts/                  drafts and pending proposals; ignored by the gate
.lawkeep/                     local state (session marker); not committed
```

Commit everything except `.lawkeep/`. Anything under `laws/` and `lawkeep.toml` counts as part of
the laws: changing a generator or the number of examples can weaken every law, so those changes
are recorded like any other.

## Commands

Every command takes `--json` and `--root PATH`.

| Command | What it does | Result codes |
| --- | --- | --- |
| `lawkeep init [--agent NAME ...]` | set up the project and agent adapters | 0, 3 |
| `lawkeep draft <module> [--request TEXT] [--docs PATH ...]` | a separate session of your agent drafts laws into `laws/drafts/`; each draft is run on today's code and quick-checked. If the agent edits an approved law, an existing generator or `lawkeep.toml`, the approved version is put back and the edit becomes a waiting proposal (never applied on its own, even when stricter; `data.waiting_changes` in JSON) | 2 drafts or changes waiting, 1 a draft fails or is weak, 3 |
| `lawkeep approve [ID ...] [--yes \| --no] [--by NAME]` | with no ids: one waiting list of five kinds (new drafts, proposed law changes, drafting-agent edits, law files changed outside lawkeep, approvals made without a terminal), each with a plain explanation and a suggestion; in a terminal it asks one by one, offers "approve all recommended new drafts and stricter changes" (never anything looser, unclear, unrecorded, unconfirmed or by the drafting agent), and asks confirm / undo / skip for an approval made without a terminal. Records every decision (`--yes` without ids never decides a weakening change, a drafting-agent edit or an unconfirmed approval) | 0 decided, 2 still waiting (no terminal), 3 |
| `lawkeep check [--profile NAME] [--law ID ...] [--base REF] [--details]` | integrity check, then every law in a clean process | 0, 1, 2, 3 |
| `lawkeep strength [--full] [--law ID ...] [--details]` | quick check with deliberately broken function versions; `--full` plants more small bugs | 0, 1 some law weak, 3 |
| `lawkeep propose <ID> --file PATH --reason TEXT` | submit a new version of a law; lawkeep decides the direction by evidence and applies the permission | 0 applied, 2 waiting, 1 rejected, 3 |
| `lawkeep report [--since ISO \| --session] [--base REF]` | law changes since a point in time, integrity issues, and the protection report | 0, 2, 3 |
| `lawkeep hook <event>` | agent and git hooks: `session-start`, `session-end`, `pre-commit`, `pre-edit` | event-specific |
| `lawkeep mcp` | MCP server with `check`, `propose`, `report` | 0, 3 |

Result codes: **0** all laws hold · **1** some laws fail · **2** waiting for a person's decision ·
**3** setup problem. When several apply, 3 wins over 1, 1 over 2, 2 over 0.

## How `lawkeep check` runs

1. **Integrity check, no code runs.** Every fingerprinted file is compared with `laws.lock`, and
   `laws.lock` with the history. Mismatches are *unrecorded law changes*. Side doors are flagged:
   `conftest.py` outside `laws/`, `sitecustomize.py`, `usercustomize.py`, `.pth` files, stray
   `.pyc` files near the laws, `skip`/`xfail` markers in laws, removed lines in the append-only
   files.
2. **A fresh pytest process** runs only `laws/` (never your normal test suite), with outside
   `conftest.py` files and auto-loaded plugins switched off and only lawkeep's plugin loaded.
   Python starts with `-I -S -B` through lawkeep's small start-up script, so no
   `sitecustomize.py`, `usercustomize.py`, `.pth` code line, `PYTHONPATH` or cached bytecode
   takes part. This is lawkeep's one law-running core (`lawkeep/lawrun.py`): `strength` and
   the example tables of `draft`/`approve`/`propose` run laws through the very same start-up,
   just in a different mode. The request goes in on the process's stdin; results come back
   through a private pipe that is written once and closed, and no `LAWKEEP_*` variable except
   `LAWKEEP_ROOT` is left in the environment when your code is imported.
   **Your code under test runs in that same process and is trusted**: code written to cheat
   could still change the result from inside. The integrity check flags source files that
   import lawkeep, pytest or Hypothesis, read `LAWKEEP_*`/`HYPOTHESIS_*` settings, raise skips,
   define comparisons that always return `True`, or shadow a standard-library or installed
   module (an error when the file changed since the git base). A skip raised while a law runs,
   or `assume()` called from outside `laws/`, makes that law an error.
3. Each law replays its saved failing inputs, then tries new ones with Hypothesis (a fixed seed in
   the `pr` profile, so the same code always gets the same verdict), shrinks any failure to the
   simplest input and saves it.
4. Output: plain English (verdict first, a table of laws, each failure with the input that
   breaks it, then the next step; `--details` adds law ids, timings and every note), or one
   JSON document with `--json`. Colour and boxes appear only in a terminal; the output is plain
   text when it is not a terminal, in CI, or with `NO_COLOR` (`LAWKEEP_COLOR=always` forces
   colour). The wording rules are in [docs/OUTPUT-STYLE.md](../OUTPUT-STYLE.md).

A law that fails reports its simplest failing input. An agent should fix the code and run
`check` again, or stop and ask (`lawkeep propose`) if it believes the law is wrong. Stopping to
ask is a normal outcome.

## `lawkeep.toml`

```toml
[permissions]            # "auto" = go ahead and tell me; "ask" = wait for my yes
add_law       = "auto"   # stricter
add_example   = "auto"   # stricter
rename_target = "auto"   # same meaning, confirmed by lawkeep
change_law    = "ask"    # unclear
weaken_law    = "ask"    # looser: always "ask" (setting "auto" is ignored with a warning)

[check]
profile = "pr"

[check.pr]               # pull requests and everyday runs
examples    = 200
derandomize = true       # fixed seed: same code, same verdict

[check.deep]             # nightly: more inputs, a new seed each run
examples    = 2000
derandomize = false
# backend = "crosshair"  # L3, experimental, needs lawkeep[deep]

[project]
source = ["src"]         # where the code under test lives

[strength]
mutation_tool   = "mutmut"
quick_check     = true
min_bugs_caught = 1

[agent]                  # the agent `lawkeep draft` starts in a separate session
name = "claude-code"     # claude-code | codex | cursor | copilot | other
# command = "..."        # override; {prompt} / {prompt_file} placeholders, else stdin
```

Unknown keys produce a warning with a "did you mean" hint.

## Permissions and directions

When an agent proposes a law change, lawkeep runs the old and new versions on the same inputs and
the same planted bugs:

| Evidence | Direction | Permission used |
| --- | --- | --- |
| new version rejects more cases / catches more bugs | stricter | `add_law` (or `add_example`) |
| identical results (e.g. after a rename) | same | `rename_target` |
| accepts cases the old one rejected | looser | `weaken_law` (always asks) |
| mixed | unclear | `change_law` |

Deleting a law, skipping it, or lowering the number of examples is always looser.

## Long runs without a person

For overnight or CI runs, agents follow **park and keep going** (in the `AGENTS.md` snippet,
the session-start reminder and the MCP instructions): when a law blocks them and nobody is
there, they `lawkeep propose` the change with a reason (looser or unclear changes wait), never
work around or edit the law, never run `lawkeep approve`, finish every part of the task that
does not depend on that law, and end with a summary: done / waiting for the person (ids) / not
done because blocked. When a person is there, they still stop and ask.

How an agent knows: `person_present` and `person_present_reason` in `check --json`,
`report --json`, `approve --json` and the session-start hook. It is a guess, first match wins:

| Signal | `person_present` |
| --- | --- |
| `LAWKEEP_PERSON_PRESENT=1` / `0` (set it in your overnight script) | true / false |
| `CI` set | false |
| standard input is a terminal | true |
| Claude Code `CLAUDE_CODE_SESSION_ATTENDED=1` / `0` | true / false |
| Claude Code `CLAUDE_CODE_ENTRYPOINT=cli` / `sdk-*` (`claude -p`) | true / false |
| anything else (Codex, Cursor, Copilot, scripts) | false |

The Claude Code variables were seen in Claude Code 2.1, not taken from a documented contract.
Other agents set nothing lawkeep recognises, so their interactive sessions read as false; the
instructions say a person chatting with the agent counts as present. The guess never changes
what lawkeep allows. `propose --json` adds `next_step` ("Parked as h-… Continue with work that
does not depend on this law; list h-… in your final summary …" when nobody is there).

Everything waiting is counted in the additive JSON field `waiting_for_person` (`count`, `ids`,
`by_kind`, `items`) of `check`, `report` and `approve`, and shown as one line ("3 things wait
for your decision (…) — run `lawkeep approve`") in `check`, `report`, the PR comment and the
session-start/end hooks. It never changes a result code; 2 still means "waiting for a
person's decision". An approval made without a terminal stays on the list until someone in a
terminal confirms it (`confirm_approval` in the history) or undoes it (`undo_approval`: the law
files go back, using a copy lawkeep kept in `laws/drafts/proposals/undo/` or the version in
git; an undone draft returns to `laws/drafts/`).

## Writing laws

```python
from lawkeep import example, given, law, settings, st

@law(
    "A discount code never raises the price and never takes it below zero",
    inputs={"price": "the price before the discount", "code": "the discount code typed in"},
)
@given(price=st.money(0, 1000), code=st.discount_codes())
@example(price=Decimal("3.00"), code="FIVEOFF")       # a fixed case that always runs
def discount_stays_between_zero_and_price(price, code):
    assert 0 <= apply_discount(price, code) <= round_money(price)
```

- `@law` goes on any pytest test (above or below `@given`) or on a Hypothesis
  `RuleBasedStateMachine`. The law id is the file path inside `laws/` plus the function name:
  `laws/discounts.py::discount_stays_between_zero_and_price` → `discounts.discount_stays_between_zero_and_price`.
- `inputs=` (optional) says in plain words what each input is; people see it next to the
  example table when they approve the law. Without it lawkeep writes something like
  "a sample discount code that lawkeep made up for testing". `lawkeep draft` asks the agent to
  always fill it in.
- `st` is `hypothesis.strategies` plus `st.money()`, `st.percentages()`, `st.non_empty_text()`,
  `st.ids()`, and anything you define in `laws/strategies.py`. Names that Hypothesis already has
  (for example `times`, `dates`, `text`) always mean Hypothesis's version; give your own
  generators different names (`clock_times`, not `times`).
- See [recipes.md](recipes.md) for patterns.

## Continuous integration

Use the GitHub Action from this repository ([action.yml](../../action.yml)):

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- uses: lawkeep/lawkeep@v0
  with:
    fail-on-broken-laws: "false"
```

| Input | Default | Meaning |
| --- | --- | --- |
| `working-directory` | `.` | folder with `lawkeep.toml` |
| `python-version` | `3.12` | Python for the laws |
| `lawkeep-spec` | `lawkeep` | what to install (version pin, path or git URL) |
| `install-project` | `true` | install the project first (pyproject/setup.py, else requirements.txt) |
| `extra-packages` | | more requirements, e.g. `lawkeep[deep]` |
| `profile` | from `lawkeep.toml` | e.g. `deep` for nightly runs |
| `base-ref` | the pull request's base commit | what to compare against |
| `comment` | `true` | post or update the one lawkeep comment |
| `fail-on-broken-laws` | `false` | fail the job on result code 1 |
| `fail-on-waiting` | `false` | fail the job on result code 2 |

Outputs: `result-code`, `verdict`, `summary`, `check-json`, `report-markdown`, `comment-url`.
The job fails on a setup problem (code 3) and otherwise only when you opt in. The workflow needs
`pull-requests: write` to comment; on pull requests from forks the token is read-only, so the
report goes to the job summary instead. Every run uses a fresh checkout, so stale local files
(like cached `.pyc`) never reach the gate.

Templates: [.github/workflows/templates/](../../.github/workflows/templates/) (pull requests and
a nightly `--profile deep` run). Without GitHub Actions, run
`lawkeep check --json` in any CI and use the result code.

## Agents

- **Any agent:** the `AGENTS.md` snippet tells it to run `lawkeep check` before finishing, to
  change laws only through `lawkeep propose`, to stop and ask when a law blocks the task and a
  person is there, and to park and keep going when nobody is (see above).
  The git pre-commit hook shows unrecorded law changes (it does not block by default).
- **MCP:** `lawkeep mcp` serves `check`, `propose` and `report`, returning the same JSON as the
  CLI.
- **Richer adapters** (Claude Code, Codex, Cursor, Copilot) use the agent's own hooks and
  permission prompt where available: session start/end reports, and direct edits to `laws/`
  turned into proposals.
