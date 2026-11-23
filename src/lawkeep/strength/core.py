"""Law strength: coverage map -> quick check -> planted bugs -> weak data -> gaps.

Two runs of the law-running core in its ``strength`` mode (each a fresh, isolated pytest
process, see :mod:`lawkeep.lawrun` and :mod:`lawkeep.strength._worker`):

1. **map** - every law runs once under coverage.py (one coverage context per law) with
   Hypothesis statistics switched on. This gives the functions each law executes, the lines it
   never reaches, how many generated inputs were thrown away, and whether it holds today.
2. **probe** - for each law that holds today: swap each function it calls by name for dumb
   versions (quick check) and plant small bugs (planted-bug check), re-running the law each time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lawkeep import discovery, exitcodes, lawrun, paths
from lawkeep.config import Config
from lawkeep.models import QuickCheckOutcome, StrengthReport, StrengthResult
from lawkeep.strength import mutants as mut
from lawkeep.strength import sourcemap as sm
from lawkeep.strength.types import CatchMatrix, PlantedBug

#: Hypothesis examples per law run: quick mode / full mode (capped by the check profile).
QUICK_EXAMPLES = 50
FULL_EXAMPLES = 200
#: Planted bugs per function in quick mode (spread over the function).
QUICK_BUGS_PER_FUNCTION = 3
#: At most this many functions per law get the quick check.
MAX_QUICK_FUNCTIONS = 8
#: Per-run time limit floor (seconds) and multiplier of the law's normal run time.
RUN_TIMEOUT_FLOOR = 5.0
RUN_TIMEOUT_FACTOR = 20.0
RUN_TIMEOUT_CAP = 120.0
MAP_TIMEOUT = 600.0
PROBE_TIMEOUT_CAP = 3600.0

CAUGHT = ("fails", "error", "timeout")
#: pytest options of the ``strength`` mode (on top of ``lawrun.pytest_args``).
STRENGTH_PYTEST_OPTIONS = ["-W", "ignore::pytest.PytestAssertRewriteWarning"]


def run_worker(
    root: Path,
    cfg: Config,
    targets: list[str],
    request: dict[str, Any],
    *,
    timeout: float | None = None,
) -> lawrun.LawRunResult:
    """One ``strength`` run of the law-running core (``request["kind"]``: "map" or "probe")."""
    return lawrun.run(
        root,
        cfg,
        "strength",
        {"strength": request},
        pytest_targets=targets,
        pytest_extra=STRENGTH_PYTEST_OPTIONS,
        timeout=timeout,
    )


def worker_problem(run: lawrun.LawRunResult) -> str | None:
    """A plain-English problem when the worker did not finish normally (None = fine)."""
    fatal = run.of_type("fatal")
    if fatal:
        return f"the strength worker stopped with an error: {fatal[0].get('message')}"
    if run.timed_out:
        return "the strength check ran out of time; results below are partial"
    if not run.finished:
        tail = run.output_tail(3).splitlines()
        return "the strength worker stopped unexpectedly" + (
            f": {' / '.join(tail)}" if tail else ""
        )
    return None


@dataclass
class LawMap:
    key: str
    law_id: str
    sentence: str | None
    file: str
    kind: str = "function"
    status: str = "not_run"
    message: str | None = None
    duration: float = 0.0
    names: list[str] = field(default_factory=list[str])
    stats: dict[str, Any] | None = None
    lines: dict[str, set[int]] = field(default_factory=dict[str, set[int]])
    touched: list[str] = field(default_factory=list[str])
    direct: list[str] = field(default_factory=list[str])

    @property
    def holds(self) -> bool:
        return self.status == "holds"


@dataclass
class MapResult:
    laws: dict[str, LawMap] = field(default_factory=dict[str, "LawMap"])
    statements: dict[str, set[int]] = field(default_factory=dict[str, set[int]])
    index: sm.SourceIndex | None = None
    notes: list[str] = field(default_factory=list[str])
    collection_errors: list[str] = field(default_factory=list[str])

    def touched_by_any(self) -> set[str]:
        out: set[str] = set()
        for law in self.laws.values():
            out.update(law.touched)
        return out

    def laws_by_function(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, law in self.laws.items():
            for name in law.touched:
                out.setdefault(name, []).append(key)
        return {k: sorted(v) for k, v in sorted(out.items())}


# ---------------------------------------------------------------------------------------------
# helpers


def law_targets(root: Path, *, include_drafts: bool = False) -> list[str]:
    """Law files (relative paths) the strength check runs."""
    return [
        paths.rel(root, p) for p in discovery.iter_python_files(root, include_drafts=include_drafts)
    ]


def examples_for(cfg: Config, full: bool) -> int:
    try:
        configured = cfg.check.get_profile().examples
    except Exception:
        configured = FULL_EXAMPLES
    return max(1, min(configured, FULL_EXAMPLES if full else QUICK_EXAMPLES))


def _run_timeout(law: LawMap) -> float:
    return min(RUN_TIMEOUT_CAP, max(RUN_TIMEOUT_FLOOR, law.duration * RUN_TIMEOUT_FACTOR))


def _is_dunder(name: str) -> bool:
    short = name.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
    return short.startswith("__") and short.endswith("__")


def _collection_note(rec: dict[str, Any]) -> str:
    where = rec.get("file") or "a law file"
    return f"{where} could not be loaded, so its laws were not measured: {rec.get('message')}"


# ---------------------------------------------------------------------------------------------
# step 1: the coverage map


def build_map(
    root: Path,
    cfg: Config,
    targets: list[str],
    *,
    law_ids: list[str] | None = None,
    examples: int = QUICK_EXAMPLES,
) -> MapResult:
    root = Path(root).resolve()
    index = sm.build_index(root, cfg)
    result = MapResult(index=index)
    if not targets:
        return result
    run = run_worker(
        root,
        cfg,
        targets,
        {
            "kind": "map",
            "law_ids": law_ids,
            "examples": examples,
            "timeout": RUN_TIMEOUT_CAP,
        },
        timeout=MAP_TIMEOUT,
    )
    problem = worker_problem(run)
    if problem:
        result.notes.append(problem)
    for rec in run.of_type("collection_error"):
        result.collection_errors.append(_collection_note(rec))
    for rec in run.of_type("note") + run.of_type("worker_error"):
        result.notes.append(f"lawkeep: {rec.get('message')}")
    for rec in run.of_type("law"):
        result.laws[rec["key"]] = LawMap(
            key=rec["key"],
            law_id=rec["law_id"],
            sentence=rec.get("sentence"),
            file=rec.get("file", ""),
            kind=rec.get("kind", "function"),
            status=rec.get("status", "not_run"),
            message=rec.get("message"),
            duration=float(rec.get("duration") or 0.0),
            names=list(rec.get("names") or []),
            stats=rec.get("stats"),
        )
    for rec in run.of_type("statements"):
        result.statements[rec["file"]] = set(rec.get("lines") or [])
    for rec in run.of_type("coverage"):
        law = result.laws.get(rec["key"])
        if law is None:
            continue
        rec_lines: dict[str, list[int]] = rec.get("lines") or {}
        law.lines = {f: set(v) for f, v in rec_lines.items()}
    for law in result.laws.values():
        touched: set[str] = set()
        for f, lines in law.lines.items():
            funcs = index.by_file.get(f)
            if not funcs:
                continue
            for line in lines:
                fn = sm.function_running(funcs, line)
                if fn is not None:
                    touched.add(index.name_of(f, fn.qualname))
        law.touched = sorted(touched)
        names = set(law.names)
        direct = [
            n
            for n in law.touched
            if not _is_dunder(n) and n.rsplit(":", 1)[-1].rsplit(".", 1)[-1] in names
        ]
        if not direct:
            direct = [n for n in law.touched if not _is_dunder(n)]
        law.direct = direct[:MAX_QUICK_FUNCTIONS]
    return result


# ---------------------------------------------------------------------------------------------
# step 2: probes (quick check and planted bugs)


def bugs_for(root: Path, index: sm.SourceIndex, name: str, limit: int | None) -> list[PlantedBug]:
    fn = index.get(name)
    if fn is None:
        return []
    try:
        source = (Path(root) / fn.file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if fn.uses_zero_arg_super:
        return []  # a method using super() cannot be re-created outside its class body
    found = mut.spread(mut.list_mutations(source, fn.qualname), limit)
    return [
        PlantedBug(
            id=f"{fn.file}::{fn.qualname}#{m.index}",
            function=name,
            file=fn.file,
            line=m.line,
            description=m.description,
        )
        for m in found
    ]


def _bug_index(bug: PlantedBug) -> int:
    return int(bug.id.rsplit("#", 1)[-1])


def run_probes(
    root: Path,
    cfg: Config,
    targets: list[str],
    mapping: MapResult,
    *,
    quick: dict[str, list[str]],
    bugs: dict[str, list[PlantedBug]],
    examples: int,
) -> lawrun.LawRunResult | None:
    """Run the quick check (``quick``: law key -> function names) and planted bugs
    (``bugs``: law key -> bugs) in one worker. Returns None when there is nothing to do."""
    index = mapping.index
    assert index is not None
    probes: dict[str, Any] = {}
    budget = 120.0
    for key, law in mapping.laws.items():
        q = quick.get(key) or []
        b = bugs.get(key) or []
        if not q and not b:
            continue
        timeout = _run_timeout(law)
        plan: dict[str, Any] = {"timeout": timeout, "quick": [], "mutants": []}
        for name in q:
            fn = index.get(name)
            if fn is not None:
                plan["quick"].append([fn.file, fn.qualname, sm.module_name(root, cfg, fn.file)])
        for bug in b:
            fn = index.get(bug.function)
            if fn is not None:
                plan["mutants"].append(
                    [fn.file, fn.qualname, sm.module_name(root, cfg, fn.file), _bug_index(bug)]
                )
        runs = len(plan["quick"]) * 5 + len(plan["mutants"])
        budget += runs * min(timeout, max(2.0, law.duration * 3 + 1.0))
        probes[key] = plan
    if not probes:
        return None
    return run_worker(
        root,
        cfg,
        targets,
        {
            "kind": "probe",
            "law_ids": None,
            "examples": examples,
            "probes": probes,
            "timeout": RUN_TIMEOUT_CAP,
        },
        timeout=min(PROBE_TIMEOUT_CAP, budget),
    )


# ---------------------------------------------------------------------------------------------
# step 3: turning records into results


def _source_line(root: Path, file: str, line: int) -> str:
    try:
        text = (Path(root) / file).read_text(encoding="utf-8").splitlines()[line - 1]
    except (OSError, IndexError, UnicodeDecodeError):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= 60 else text[:57] + "..."


def _ranges(lines: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(lines):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def _unreached(root: Path, mapping: MapResult, law: LawMap) -> list[str]:
    index = mapping.index
    notes: list[str] = []
    if index is None:
        return notes
    for name in law.direct:
        fn = index.get(name)
        if fn is None:
            continue
        stmts = {n for n in mapping.statements.get(fn.file, set()) if fn.body_contains(n)}
        ran = law.lines.get(fn.file, set())
        if not stmts or not (stmts & ran):
            continue
        missing = sorted(stmts - ran)
        if not missing:
            continue
        parts: list[str] = []
        for a, b in _ranges(missing)[:3]:
            where = f"line {a}" if a == b else f"lines {a}-{b}"
            code = _source_line(root, fn.file, a)
            parts.append(f"{where} (`{code}`)" if code else where)
        notes.append(
            f"The inputs this law tried never reached {', '.join(parts)} of {sm.short(name)}; "
            f"bugs there would go unnoticed."
        )
    return notes


def _weak_data(law: LawMap) -> tuple[list[str], list[str]]:
    """(weak_data notes, flags) from Hypothesis statistics."""
    s = law.stats
    if not s:
        return [], []
    notes: list[str] = []
    flags: list[str] = []
    generated = int(s.get("generated") or 0)
    invalid = int(s.get("invalid") or 0)
    valid = int(s.get("valid") or 0)
    if generated >= 10 and invalid / generated >= 0.5:
        pct = round(100 * invalid / generated)
        flags.append(
            f"Weak test data: {pct}% of the generated inputs were thrown away (by assume() or "
            f".filter()), so only {valid} inputs were really tried. Generate valid inputs "
            f"directly, e.g. st.integers(min_value=1) instead of assume(x > 0)."
        )
    events: dict[str, int] = s.get("events") or {}
    for event, count in events.items():
        if generated and count / generated >= 0.2 and not str(event).startswith("Retried draw"):
            notes.append(f"{round(100 * count / generated)}% of inputs: {event}")
    return notes, flags


def _quick_outcomes(
    probe: lawrun.LawRunResult | None, key: str, index: sm.SourceIndex
) -> list[QuickCheckOutcome]:
    out: list[QuickCheckOutcome] = []
    if probe is None:
        return out
    for rec in probe.of_type("quick"):
        if rec.get("key") != key or rec.get("variant") is None:
            continue
        name = index.name_of(rec["file"], rec["qualname"])
        caught = rec.get("status") in CAUGHT
        desc = rec.get("description") or rec["variant"]
        note = f"law fails when {desc}" if caught else f"law still holds when {desc}"
        if rec.get("status") == "timeout":
            note = f"law ran too long when {desc} (counted as caught)"
        out.append(
            QuickCheckOutcome(function=name, variant=rec["variant"], caught=caught, note=note)
        )
    return out


def _one_sided_flags(outcomes: list[QuickCheckOutcome]) -> tuple[list[str], bool, str | None]:
    """(flags, vacuous, vacuity_reason) from quick-check outcomes."""
    flags: list[str] = []
    by_fn: dict[str, list[QuickCheckOutcome]] = {}
    for o in outcomes:
        by_fn.setdefault(o.function, []).append(o)
    if not by_fn:
        return flags, False, None
    all_missed = all(not o.caught for o in outcomes)
    if all_missed:
        fns = ", ".join(sm.short(f) for f in by_fn)
        reason = (
            f"This law still holds whatever {fns} does (always None, always 0, its input "
            f"unchanged, even always an error): it does not really check {fns}."
        )
        return (
            [f"Vacuous: {reason} Make the law call the function and check its result."],
            True,
            reason,
        )
    for fn, items in by_fn.items():
        missed = [o for o in items if not o.caught]
        if not missed:
            continue
        short = sm.short(fn)
        value_missed = [o for o in missed if o.variant != "raises"]
        if value_missed:
            first = (
                value_missed[0].note.replace("law still holds when ", "")
                if value_missed[0].note
                else ""
            )
            also = [o.note.replace("law still holds when ", "") for o in value_missed[1:] if o.note]
            more = f" (also when {'; '.join(also)})" if also else ""
            flags.append(
                f"One-sided: this law still holds when {first}{more}. It checks a limit, not "
                f"the answer. Suggestion: add a law that pins down what {short} returns, e.g. "
                f"an exact result for a known input, or a relation between input and output."
            )
        if any(o.variant == "raises" for o in missed):
            flags.append(
                f"This law still holds when {short} always raises an error: it may catch the "
                f"error itself (try/except, pytest.raises) or skip the check when the call fails."
            )
    return flags, False, None


def assemble(
    root: Path,
    cfg: Config,
    mapping: MapResult,
    probe: lawrun.LawRunResult | None,
    bugs: dict[str, list[PlantedBug]],
) -> list[StrengthResult]:
    index = mapping.index
    assert index is not None
    results: list[StrengthResult] = []
    one_sided: dict[str, list[str]] = {}  # law key -> its one-sided / swallowed-error flags
    sources = ", ".join(paths.rel(root, d) or "." for d in cfg.project.source_dirs(root))
    for key, law in sorted(mapping.laws.items()):
        r = StrengthResult(law_id=key, sentence=law.sentence, functions_touched=list(law.touched))
        if not law.holds:
            why = f" ({law.message})" if law.message else ""
            r.flags.append(
                f"This law does not hold on today's code{why}, so its strength was not "
                f"measured. Run `lawkeep check` to see the simplest failing example."
            )
            results.append(r)
            continue
        if not law.touched:
            r.vacuous = True
            r.vacuity_reason = (
                f"This law never ran any function in your project's code ({sources}), so it "
                f"cannot catch a bug there."
            )
            r.flags.append(
                f"Vacuous: {r.vacuity_reason} Check that it calls your code, or set "
                f"[project] source in lawkeep.toml."
            )
        r.quick_check = _quick_outcomes(probe, key, index)
        qflags, vac, reason = _one_sided_flags(r.quick_check)
        if vac:
            r.vacuous, r.vacuity_reason = True, reason
        elif qflags:
            one_sided[key] = list(qflags)
        r.flags.extend(qflags)
        planned = bugs.get(key) or []
        if planned and probe is not None:
            by_id = {b.id: b for b in planned}
            total = caught = 0
            for rec in probe.of_type("mutant"):
                if rec.get("key") != key:
                    continue
                bug = by_id.get(f"{rec['file']}::{rec['qualname']}#{rec['index']}")
                if bug is None or rec.get("status") == "not_planted":
                    continue
                total += 1
                if rec.get("status") in CAUGHT:
                    caught += 1
                else:
                    r.missed_bugs.append(
                        {
                            "file": bug.file,
                            "line": bug.line,
                            "description": bug.description,
                            "function": bug.function,
                        }
                    )
            r.planted_bugs_total, r.planted_bugs_caught = total, caught
            if total and caught < max(1, cfg.strength.min_bugs_caught):
                example = r.missed_bugs[0] if r.missed_bugs else None
                ex = (
                    f" For example, in {sm.short(example['function'])} line {example['line']}: "
                    f"{example['description']} - and the law still holds."
                    if example
                    else ""
                )
                r.flags.append(f"Catches {caught} of {total} planted bugs.{ex}")
        notes, wflags = _weak_data(law)
        r.weak_data.extend(notes)
        r.weak_data.extend(_unreached(root, mapping, law))
        r.flags.extend(wflags)
        results.append(r)
    drafts = {k for k, law in mapping.laws.items() if law.file.startswith("laws/drafts/")}
    _credit_companions(results, one_sided, drafts)
    return results


def _credit_companions(
    results: list[StrengthResult], one_sided: dict[str, list[str]], drafts: set[str]
) -> None:
    """A law that checks a limit ("a refund never exceeds the payment") is fine when other laws
    in the same run pin down the exact answer: if every deliberately broken version it lets
    through is caught by another approved law, its one-sided flags become a note instead.
    Drafts never count as companions: the gate does not check them."""
    for r in results:
        flags = one_sided.get(r.law_id)
        if not flags:
            continue
        missed = {(o.function, o.variant) for o in r.quick_check if not o.caught}
        helpers: set[str] = set()
        for fn, variant in missed:
            by = [
                other.law_id
                for other in results
                if other is not r
                and other.law_id not in drafts
                and not other.vacuous
                and any(
                    o.function == fn and o.variant == variant and o.caught
                    for o in other.quick_check
                )
            ]
            if not by:
                break
            helpers.update(by)
        else:
            r.flags = [f for f in r.flags if f not in flags]
            r.weak_data.insert(
                0,
                "On its own this law checks a limit, not the exact answer, but other laws catch "
                f"every broken version it lets through: {', '.join(sorted(helpers))}.",
            )


# ---------------------------------------------------------------------------------------------
# public entry points


def analyse(
    root: Path,
    cfg: Config,
    *,
    targets: list[str],
    law_ids: list[str] | None = None,
    full: bool = False,
    sample_bugs: int | None = QUICK_BUGS_PER_FUNCTION,
    quick: bool = True,
) -> tuple[list[StrengthResult], MapResult, list[str]]:
    root = Path(root).resolve()
    examples = examples_for(cfg, full)
    mapping = build_map(root, cfg, targets, law_ids=law_ids, examples=examples)
    notes = list(mapping.notes) + list(mapping.collection_errors)
    assert mapping.index is not None
    quick_plan: dict[str, list[str]] = {}
    bug_plan: dict[str, list[PlantedBug]] = {}
    cache: dict[str, list[PlantedBug]] = {}
    for key, law in mapping.laws.items():
        if not law.holds:
            continue
        if quick and cfg.strength.quick_check:
            quick_plan[key] = list(law.direct)
        fns = law.touched if full else law.direct
        limit = None if full else sample_bugs
        if not full and not sample_bugs:
            continue
        planned: list[PlantedBug] = []
        for name in fns:
            if name not in cache:
                cache[name] = bugs_for(root, mapping.index, name, limit)
            planned.extend(cache[name])
        if planned:
            bug_plan[key] = planned
    if not cfg.strength.quick_check:
        notes.append(
            "The quick check (dumb versions) is switched off in lawkeep.toml ([strength] quick_check)."
        )
    probe = run_probes(
        root, cfg, targets, mapping, quick=quick_plan, bugs=bug_plan, examples=examples
    )
    if probe is not None:
        problem = worker_problem(probe)
        if problem:
            notes.append(problem)
        for rec in probe.of_type("worker_error"):
            notes.append(f"lawkeep: {rec.get('message')}")
    return assemble(root, cfg, mapping, probe, bug_plan), mapping, notes


def uncovered_functions(mapping: MapResult) -> list[str]:
    index = mapping.index
    if index is None:
        return []
    touched = mapping.touched_by_any()
    out: list[str] = []
    for name, fn in sorted(index.by_name.items()):
        if not fn.is_public or name in touched:
            continue
        if any(d in ("overload", "abstractmethod") for d in fn.decorators):
            continue
        out.append(name)
    return out


def run_quick_check(
    root: Path,
    cfg: Config,
    *,
    law_ids: list[str] | None = None,
    include_drafts: bool = False,
    sample_bugs: int | None = QUICK_BUGS_PER_FUNCTION,
) -> list[StrengthResult]:
    """Swap each function a law calls for dumb versions and plant a few bugs; flag weak laws.

    ``include_drafts=True`` also measures ``laws/drafts/`` (used by ``draft``/``approve``).
    When an approved law and a draft share an id, the result ids are ``"<id>@<file>"``.
    """
    root = Path(root).resolve()
    results, _mapping, _notes = analyse(
        root,
        cfg,
        targets=law_targets(root, include_drafts=include_drafts),
        law_ids=law_ids,
        full=False,
        sample_bugs=sample_bugs,
    )
    return results


def run_strength(
    root: Path,
    cfg: Config,
    *,
    law_ids: list[str] | None = None,
    full: bool = False,
    include_drafts: bool = False,
) -> StrengthReport:
    root = Path(root).resolve()
    targets = law_targets(root, include_drafts=include_drafts)
    if law_ids:
        from lawkeep.discovery import discover_laws
        from lawkeep.errors import SetupError

        known = {i.law_id for i in discover_laws(root, include_drafts=include_drafts).laws}
        unknown = sorted(set(law_ids) - known)
        if unknown:
            raise SetupError(
                f"No law has the id(s) {', '.join(unknown)}, so nothing was measured. Known "
                f"laws: {', '.join(sorted(known)) or 'none'}.",
                hint="Check the spelling (`lawkeep strength` with no --law measures every law).",
            )
    report = StrengthReport(mode="full" if full else "quick")
    report.notes.extend(cfg.warnings)
    if not targets:
        report.summary = "NO LAWS YET: there is nothing to measure. Add laws with `lawkeep draft`."
        report.exit_code = exitcodes.ALL_HOLD
        report.uncovered_functions = uncovered_functions(build_map(root, cfg, []))
        return report
    results, mapping, notes = analyse(root, cfg, targets=targets, law_ids=law_ids, full=full)
    report.laws = results
    report.notes.extend(notes)
    if law_ids is None:
        report.uncovered_functions = uncovered_functions(mapping)
    else:
        report.notes.append("Functions no law runs are listed only when all laws are measured.")
    if law_ids:
        missing = [i for i in law_ids if not any(r.law_id.split("@")[0] == i for r in results)]
        for i in missing:
            report.notes.append(f"No law with id {i!r} was found in laws/.")
    if any(r.missed_bugs for r in results):
        report.notes.append(
            "A few planted bugs may not change what the code does (for example `>` vs `>=` "
            "where both sides are equal), so a missed bug is worth a look, not proof of a gap."
        )
    flagged = [r for r in results if r.flags]
    if not full:
        report.notes.append(
            f"Quick mode: up to {QUICK_BUGS_PER_FUNCTION} planted bugs per function a law calls "
            f"by name. Run `lawkeep strength --full` to plant every bug in every function."
        )
    if flagged:
        report.exit_code = exitcodes.SOME_FAIL
        report.summary = f"SOME LAWS ARE WEAK: {len(flagged)} of {len(results)} laws flagged."
    elif results:
        report.exit_code = exitcodes.ALL_HOLD
        report.summary = (
            f"NO WEAK LAWS FOUND: {len(results)} of {len(results)} laws passed the strength checks."
        )
    else:
        report.exit_code = exitcodes.ALL_HOLD
        report.summary = "NO LAWS MEASURED: no law could be run (see the notes)."
    gaps = len(report.uncovered_functions)
    if gaps:
        report.summary += f" {gaps} public function(s) are not run by any law."
    return report


def catch_matrix(
    root: Path,
    cfg: Config,
    targets: list[str],
    *,
    functions: list[str] | None = None,
    max_bugs: int | None = None,
) -> CatchMatrix:
    """Plant bugs in ``functions`` (default: every function a law runs) and record which laws
    in the ``targets`` files catch each one.

    ``functions`` are ``"module:qualname"`` names. ``max_bugs`` limits the planted bugs *per
    function* (evenly spread); None plants every bug lawkeep can make. Bugs are planted only in
    memory inside the worker process; files are never changed. Laws run only against bugs in
    functions they execute. Bugs in a function no law runs are listed but caught by nobody.
    """
    root = Path(root).resolve()
    examples = examples_for(cfg, full=True)
    mapping = build_map(root, cfg, list(targets), examples=examples)
    index = mapping.index
    assert index is not None
    matrix = CatchMatrix(notes=list(mapping.notes) + list(mapping.collection_errors))
    matrix.laws_by_function = mapping.laws_by_function()
    wanted = list(functions) if functions is not None else sorted(mapping.touched_by_any())
    by_function: dict[str, list[PlantedBug]] = {}
    for name in wanted:
        if index.get(name) is None:
            matrix.notes.append(f"{name} was not found in your project's source; no bugs planted.")
            continue
        found = bugs_for(root, index, name, max_bugs)
        fn = index.get(name)
        if not found and fn is not None and fn.uses_zero_arg_super:
            matrix.notes.append(
                f"{sm.short(name)} uses super(), so lawkeep cannot plant bugs in it yet."
            )
        by_function[name] = found
        matrix.bugs.extend(found)
    bug_plan: dict[str, list[PlantedBug]] = {}
    for key, law in mapping.laws.items():
        if not law.holds:
            matrix.failing_laws.append(key)
            continue
        planned = [b for name in law.touched for b in by_function.get(name, [])]
        if planned:
            bug_plan[key] = planned
    if matrix.failing_laws:
        matrix.notes.append(
            "These laws do not hold on today's code, so the bugs they catch are not counted: "
            + ", ".join(sorted(matrix.failing_laws))
        )
    probe = run_probes(
        root, cfg, list(targets), mapping, quick={}, bugs=bug_plan, examples=examples
    )
    if probe is not None:
        problem = worker_problem(probe)
        if problem:
            matrix.notes.append(problem)
        for rec in probe.of_type("mutant"):
            if rec.get("status") == "not_planted":
                continue
            bug_id = f"{rec['file']}::{rec['qualname']}#{rec['index']}"
            matrix.tested.setdefault(rec["key"], []).append(bug_id)
            if rec.get("status") in CAUGHT:
                matrix.caught.setdefault(rec["key"], []).append(bug_id)
    for key in mapping.laws:
        if mapping.laws[key].holds:
            matrix.caught.setdefault(key, [])
    return matrix
