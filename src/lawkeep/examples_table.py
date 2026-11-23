"""Concrete examples shown to a person: input -> result -> law holds?

Owner: workstream D (law lifecycle). Used by ``approve`` and ``draft`` (example tables, "does
this draft hold on today's code?") and by ``direction`` (old vs new law on the same inputs).

How it works (wrapping Hypothesis, not re-implementing it):

1. The law-running core runs the law in its ``probe`` mode (``lawkeep.lawrun``; the child
   code is ``lawkeep._probe_worker``): a fresh isolated Python process imports the law file(s)
   with the project's source folders on ``sys.path`` and ``LAWKEEP_ROOT`` set, so
   ``st.<project generator>()`` works. Bytecode writing is off, so no ``.pyc`` lands near laws.
2. For a Hypothesis law, lawkeep swaps the law's ``.hypothesis.inner_test`` (Hypothesis
   documents this attribute as reassignable) for a recorder and runs the law with fixed-seed
   settings. Hypothesis draws the inputs (explicit ``@example``s first, then generated ones,
   which start with simple edge cases such as 0 and empty); the recorder runs the real law
   body on each input and never lets a failure stop the run, so we see many inputs.
3. Functions of the project that the law calls (``from billing import refund``) are wrapped
   so the table can show what the real code returned ("refund() -> 40.00"). The same wrappers
   can return deliberately broken results ("always 0", "1 more than it should") so two law
   versions can be compared on code that is wrong (see ``lawkeep.direction``).
4. Laws whose structure cannot be introspected (pytest fixtures, unusual wrappers) fall back
   to "run the law with Hypothesis and report whether it held"; state machines are run with
   Hypothesis' own ``run_state_machine_as_test`` and shown as one row.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lawkeep import discovery, lawrun
from lawkeep.config import Config
from lawkeep.models import ExampleRow
from lawkeep.output import table
from lawkeep.paths import drafts_dir, laws_dir, rel

#: Draft input generators waiting for approval (see commands/draft.py).
DRAFT_STRATEGIES = "strategies.py"
PROBE_TIMEOUT_SECONDS = 300

# ---------------------------------------------------------------------------------------------
# Parent side


@dataclass
class LawProbe:
    """What lawkeep learned by running one law on varied inputs (no pytest involved)."""

    law_id: str
    sentence: str | None = None
    status: str = "not_run"  # "holds" | "fails" | "error" | "not_run"
    examples_tried: int = 0
    rows: list[ExampleRow] = field(
        default_factory=list["ExampleRow"]
    )  # a varied selection for people
    failing: list[ExampleRow] = field(
        default_factory=list["ExampleRow"]
    )  # inputs where the law failed
    message: str | None = None  # why it could not run / first failure message
    notes: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        return {
            "law_id": self.law_id,
            "sentence": self.sentence,
            "status": self.status,
            "examples_tried": self.examples_tried,
            "rows": [r.to_dict() for r in self.rows],
            "failing": [r.to_dict() for r in self.failing],
            "message": self.message,
            "notes": list(self.notes),
        }


def draft_strategies_file(root: Path) -> Path | None:
    p = drafts_dir(root) / DRAFT_STRATEGIES
    return p if p.is_file() else None


def run_probe(
    root: Path,
    cfg: Config,
    versions: list[dict[str, str]],
    *,
    examples: int = 60,
    mutants: bool = False,
    extra_inputs: list[dict[str, str]] | None = None,
    use_draft_strategies: bool = False,
    timeout: float | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Run the probe process. ``versions`` = ``[{"name", "file", "qualname"}]``.

    Returns the probe's JSON (``{"versions": {...}, "cases": [...], "mutants": [...]}``) or a
    dict with ``"error"`` when the process could not finish.

    The run is deterministic: Hypothesis draws from ``seed`` (default: derived from the
    versions' file contents and qualnames, see :func:`probe_seed`), with the same example
    budget, no example database, ``PYTHONHASHSEED=0`` and a private Hypothesis storage folder,
    so the same law files always give the same inputs and the same answers.
    """
    root = Path(root).resolve()
    strategies_override = draft_strategies_file(root) if use_draft_strategies else None
    if seed is None:
        seed = probe_seed(versions)
    spec = {
        "laws_dir": str(laws_dir(root).resolve()),
        "versions": versions,
        "examples": max(1, int(examples)),
        "mutants": bool(mutants),
        "extra_inputs": list(extra_inputs or []),
        "strategies_override": str(strategies_override) if strategies_override else None,
        "seed": int(seed),
    }
    limit = timeout or PROBE_TIMEOUT_SECONDS
    run = lawrun.run(root, cfg, "probe", {"probe": spec}, timeout=limit)
    if run.timed_out:
        return {
            "error": f"Running the law on examples took longer than {int(limit)} seconds "
            f"and was stopped."
        }
    rec = run.first("probe")
    if rec is None:
        if run.unreadable:
            return {"error": f"The example run wrote unreadable output ({run.unreadable[0]})."}
        tail = run.output_tail(5).splitlines()
        return {"error": "The law could not be run on examples: " + " / ".join(tail)}
    result: dict[str, Any] = rec.get("result") or {}
    return result


def probe_seed(versions: list[dict[str, str]], salt: str = "") -> int:
    """A fixed Hypothesis seed for a probe run: a hash of ``salt`` and every version's file
    contents and qualname (same files -> same seed -> same inputs)."""
    h = hashlib.sha256(salt.encode("utf-8"))
    for v in versions:
        h.update(b"\0" + v.get("qualname", "").encode("utf-8") + b"\0")
        try:
            h.update(Path(v["file"]).read_bytes())
        except (OSError, KeyError):
            h.update(v.get("file", "").encode("utf-8"))
    return int.from_bytes(h.digest()[:8], "big")


def _find_law(root: Path, law_file: Path, law_id: str):
    found = discovery.laws_in_file(law_file, root)
    for info in found.laws:
        if info.law_id == law_id:
            return info
    return None


_ZERO = re.compile(r"-?0+(\.0+)?")


def _edge_note(inputs: dict[str, str]) -> str | None:
    for value in inputs.values():
        v = value.strip()
        if _ZERO.fullmatch(v):
            return "edge case: zero"
        if v in ("''", '""', "[]", "{}", "()", "set()", "b''"):
            return "edge case: empty"
        if v == "None":
            return "edge case: nothing (None)"
    return None


def _row(case: dict[str, Any], version: str) -> ExampleRow:
    real = case["real"].get(version, {})
    outcome = real.get("outcome")
    result = real.get("result")
    if result is None and outcome == "fails" and real.get("message"):
        result = real["message"]
    note = "example written in the law" if case.get("source") == "explicit" else None
    if case.get("source") == "saved":
        note = "saved failing example"
    return ExampleRow(
        inputs=dict(case["inputs"]),
        result=result,
        holds=(outcome == "holds") if outcome in ("holds", "fails") else None,
        note=note or _edge_note(case["inputs"]),
    )


def _key(case: dict[str, Any]) -> str:
    return json.dumps(case["inputs"], sort_keys=True)


def select_varied(cases: list[dict[str, Any]], version: str, count: int) -> list[dict[str, Any]]:
    """A varied handful: failures first, written examples, the simplest inputs, then a spread."""
    usable: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in cases:
        outcome = c["real"].get(version, {}).get("outcome")
        if outcome not in ("holds", "fails"):
            continue
        k = _key(c)
        if k in seen:
            continue
        seen.add(k)
        usable.append(c)
    if len(usable) <= count:
        return usable
    chosen: list[int] = []

    def take(i: int) -> None:
        if i not in chosen and len(chosen) < count:
            chosen.append(i)

    failing = [i for i, c in enumerate(usable) if c["real"][version]["outcome"] == "fails"]
    failing.sort(key=lambda i: len(_key(usable[i])))
    for i in failing[:2]:
        take(i)
    for i, c in enumerate(usable):
        if c.get("source") in ("explicit", "saved"):
            take(i)
    for i in range(min(2, len(usable))):
        take(i)
    edge = [i for i, c in enumerate(usable) if _edge_note(c["inputs"])]
    for i in edge[:2]:
        take(i)
    remaining = [i for i in range(len(usable)) if i not in chosen]
    slots = count - len(chosen)
    if slots > 0 and remaining:
        step = len(remaining) / slots
        for n in range(slots):
            take(remaining[min(len(remaining) - 1, int(n * step))])
    return [usable[i] for i in sorted(chosen)]


def probe_law(
    root: Path,
    cfg: Config,
    law_file: Path,
    law_id: str,
    *,
    count: int = 8,
    examples: int | None = None,
    use_draft_strategies: bool | None = None,
) -> LawProbe:
    """Run one law on varied inputs: does it hold on today's code, and a few example rows."""
    root = Path(root).resolve()
    law_file = Path(law_file)
    if not law_file.is_absolute():
        law_file = root / law_file
    info = _find_law(root, law_file, law_id)
    probe = LawProbe(law_id=law_id)
    if info is None:
        probe.status = "error"
        probe.message = f"There is no law {law_id!r} in {rel(root, law_file)}."
        return probe
    probe.sentence = info.sentence
    if use_draft_strategies is None:
        use_draft_strategies = True
    n = examples if examples is not None else max(count * 6, 50)
    data = run_probe(
        root,
        cfg,
        [{"name": "law", "file": str(law_file), "qualname": info.qualname}],
        examples=n,
        use_draft_strategies=use_draft_strategies,
    )
    if "error" in data:
        probe.status = "error"
        probe.message = data["error"]
        return probe
    v = data["versions"].get("law", {})
    probe.notes.extend(v.get("notes", []))
    if v.get("status") == "import_error":
        probe.status = "error"
        probe.message = v.get("message")
        return probe
    if v.get("status") == "unsupported":
        probe.status = "not_run"
        probe.message = v.get("message")
        return probe
    cases = [c for c in data.get("cases", []) if c["real"].get("law", {}).get("outcome")]
    evaluated = [c for c in cases if c["real"]["law"]["outcome"] in ("holds", "fails")]
    probe.examples_tried = len(evaluated)
    failing = [c for c in evaluated if c["real"]["law"]["outcome"] == "fails"]
    failing.sort(key=lambda c: len(_key(c)))
    probe.failing = [_row(c, "law") for c in failing[:3]]
    probe.rows = [_row(c, "law") for c in select_varied(cases, "law", count)]
    if v.get("draw_error"):
        probe.notes.append(f"Hypothesis stopped early: {v['draw_error']}")
    if v.get("whole_run"):
        # state machine or other law run as a whole
        whole = v["whole_run"]
        probe.examples_tried = max(probe.examples_tried, 1)
        probe.status = "holds" if whole.get("outcome") == "holds" else "fails"
        probe.message = whole.get("message")
        probe.rows = [
            ExampleRow(
                inputs={},
                result=whole.get("message") or "ran as a sequence of steps",
                holds=whole.get("outcome") == "holds",
                note="this law is a sequence of steps; single examples are not shown",
            )
        ]
        return probe
    if failing:
        probe.status = "fails"
        probe.message = failing[0]["real"]["law"].get("message")
    elif evaluated:
        probe.status = "holds"
    elif v.get("draw_error"):
        probe.status = "error"
        probe.message = v["draw_error"]
    else:
        probe.status = "not_run"
        probe.message = "No usable inputs were generated (every input was filtered out)."
    return probe


def example_rows(
    root: Path, cfg: Config, law_file: Path, law_id: str, *, count: int = 8
) -> list[ExampleRow]:
    """A few varied inputs (edge cases included) from Hypothesis, run against the real code."""
    probe = probe_law(root, cfg, law_file, law_id, count=count)
    if probe.rows:
        return probe.rows
    if probe.message:
        return [ExampleRow(inputs={}, result=None, holds=None, note=probe.message)]
    return []


def _mark(value: bool | None) -> str:
    if value is True:
        return "yes ✓"
    if value is False:
        return "NO ✗"
    return "-"


def _inputs_text(inputs: dict[str, str]) -> str:
    if not inputs:
        return "(no inputs)"
    return ", ".join(f"{k}={v}" for k, v in inputs.items())


def render_rows(rows: list[ExampleRow]) -> str:
    """Plain-English table, e.g. ``charge=40, amount=50 → refund() → 40  yes ✓``."""
    if not rows:
        return "  (no examples could be shown)"
    proposal = any(r.old_holds is not None for r in rows)
    body: list[list[str]] = []
    for r in rows:
        line = [_inputs_text(r.inputs), "→", r.result if r.result is not None else "-"]
        if proposal:
            line += [_mark(r.old_holds), _mark(r.holds)]
        else:
            line += [_mark(r.holds)]
        line.append(r.note or "")
        body.append(line)
    headers = ["input", "", "result"]
    headers += ["old law holds?", "new law holds?"] if proposal else ["law holds?"]
    headers.append("note")
    return table(body, headers)
