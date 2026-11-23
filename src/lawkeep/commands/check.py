"""`lawkeep check`: run every law: ALL LAWS HOLD or SOME LAWS FAIL.

Owner: workstream B (gate).

Order: read lawkeep.toml -> integrity check (no code runs) -> run the laws in a clean pytest
process -> save newly found failing examples -> collect pending decisions -> report.

Result codes: 0 all laws hold · 1 some law fails · 2 every law holds but something needs a
person's decision (an unrecorded law change, a side door, a waiting proposal) · 3 setup problem.
`check` only reads files, except for appending newly found failing examples to
laws/failing_examples.jsonl (it never rewrites laws.lock).
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import RenderableType
from rich.text import Text

from lawkeep import config, exitcodes, integrity, ledger, output, paths, runner
from lawkeep.errors import HistoryError
from lawkeep.models import CheckReport, LawChange, LawResult, utc_now_iso

if TYPE_CHECKING:
    from lawkeep.waiting import WaitingList

HELP = "check that your code follows every law (answer: ALL LAWS HOLD or SOME LAWS FAIL)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="which settings to use: 'pr' (the default: the same inputs every run, so the same "
        "code always gets the same answer) or 'deep' (many more random inputs, for nightly runs)",
    )
    parser.add_argument(
        "--deep-check",
        action="store_true",
        help="experimental: let CrossHair pick inputs by reading the code (needs lawkeep[deep]); "
        "it may not try every path through the code",
    )
    parser.add_argument(
        "--law",
        metavar="LAW_ID",
        action="append",
        default=None,
        help="check only this law, by its id (repeat for several)",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        default=None,
        help="git branch or commit to compare with when looking for files that could get "
        "around the laws (default: HEAD)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="do not save newly found failing inputs (normally kept in "
        "laws/failing_examples.jsonl and tried again on every check)",
    )
    parser.add_argument(
        "--details",
        "-v",
        action="store_true",
        help="also show law ids, inputs tried, timings and every note",
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help="stop the laws after this many seconds (default: no limit)",
    )


def _pending(root: Path) -> list[LawChange]:
    """Decisions waiting for a person (``lawchanges.list_pending``)."""
    from lawkeep import lawchanges

    try:
        return list(lawchanges.list_pending(root))
    except HistoryError:
        return []  # already reported by the integrity check


def _save_new_failures(
    root: Path, outcome: runner.RunOutcome, profile_name: str
) -> tuple[int, list[str]]:
    saved = 0
    notes: list[str] = []
    by_law: dict[str, int] = {}
    for item in outcome.new_failures:
        try:
            before = len(ledger.load(root))
            ledger.add(
                root,
                item["law_id"],
                item.get("example"),
                found_by="lawkeep",
                choices=item.get("choices"),
                failure=item.get("failure"),
                profile=profile_name,
            )
            if len(ledger.load(root)) > before:
                saved += 1
                by_law[item["law_id"]] = by_law.get(item["law_id"], 0) + 1
        except (OSError, ValueError, KeyError) as exc:
            notes.append(f"Could not save a failing example: {exc}")
    if saved:
        laws = ", ".join(sorted(by_law))
        notes.append(
            f"lawkeep saved {saved} new failing input(s) in laws/failing_examples.jsonl ({laws}) "
            f"and will try them again on every check. Commit this file with your change."
        )
    return saved, notes


def _profile_text(report: CheckReport, results: list[LawResult]) -> str:
    no_evidence: dict[str, Any] = {}
    ev = next((r.evidence for r in results if r.evidence.get("max_examples")), no_evidence)
    if not ev:
        return f"Settings: profile {report.profile}"
    mode = (
        "the same inputs every run, so the same code gives the same result"
        if ev.get("derandomize")
        else "new random inputs every run"
    )
    backend = ev.get("backend", "hypothesis")
    extra = " · inputs chosen by CrossHair (experimental)" if backend == "crosshair" else ""
    return (
        f"lawkeep tried up to {ev.get('max_examples')} inputs per law ({mode}) · "
        f"profile {report.profile}{extra}"
    )


def _summary(report: CheckReport) -> str:
    c = report.counts()
    total = len(report.laws)
    bad = c["fails"] + c["error"]
    holds = c["holds"]
    skipped = c["skipped"] + c["not_run"]
    skip_text = f"; {skipped} skipped" if skipped else ""
    n_dec = sum(1 for i in report.integrity if i.severity == "error") + len(report.waiting)
    if report.exit_code == exitcodes.SOME_FAIL:
        return f"SOME LAWS FAIL ({bad} of {total} fail{skip_text})"
    if report.exit_code == exitcodes.WAITING:
        return (
            f"WAITING FOR YOUR DECISION ({holds} of {total} laws hold{skip_text}; "
            f"{n_dec} item(s) need a person's decision)"
        )
    if total == 0:
        return "ALL LAWS HOLD (there are no laws yet, so nothing was checked)"
    if report.only_laws:
        known = report.laws_known if report.laws_known is not None else total
        others = max(known - total, 0)
        return (
            f"ALL LAWS HOLD for the {total} law(s) checked ({holds} of {total}{skip_text}); "
            f"PARTIAL RUN: only the laws named with --law were checked, {others} other law(s) "
            f"were not run"
        )
    return f"ALL LAWS HOLD ({holds} of {total}{skip_text})"


def _next_step(report: CheckReport) -> str | None:
    """The plain next step (also used by tests and the docs)."""
    nobody = report.person_present is False
    if report.exit_code == exitcodes.SOME_FAIL:
        return (
            "fix the code so each failing law holds for the example shown, then run "
            "`lawkeep check` again. If you think a law itself is wrong, stop and ask a person "
            "(`lawkeep propose`); do not edit laws/ directly."
            + (
                " If nobody is there, propose the change with your reason, keep going with "
                "work that does not depend on that law, and list it in your final summary."
                if nobody
                else ""
            )
        )
    if report.exit_code == exitcodes.WAITING:
        return (
            "a person should look at the items above and decide (`lawkeep approve`). "
            "Stopping to ask is a normal outcome."
            + (
                " If nobody is there, keep going with work that does not depend on these "
                "items and list them in your final summary."
                if nobody
                else ""
            )
        )
    if not report.laws:
        return "add your first laws: ask your coding agent to run `lawkeep draft`."
    if report.only_laws:
        return "run `lawkeep check` without --law before saying all laws hold."
    return "nothing to do. Your laws hold."


_STATUS_WORD = {
    "holds": ("Holds", "ok"),
    "fails": ("Fails", "fail"),
    "error": ("Could not run", "fail"),
    "skipped": ("Skipped", "warn"),
    "not_run": ("Not run", "warn"),
}


def _banner(report: CheckReport):
    from lawkeep import ui

    c = report.counts()
    total = len(report.laws)
    bad = c["fails"] + c["error"]
    n_dec = sum(1 for i in report.integrity if i.severity == "error") + len(report.waiting)
    skipped = c["skipped"] + c["not_run"]
    skip = f" {ui.plural(skipped, 'law was', 'laws were')} skipped." if skipped else ""
    if report.exit_code == exitcodes.SOME_FAIL:
        return ui.banner(
            "SOME LAWS FAIL",
            f"{bad} of {ui.plural(total, 'law')} broke on your current code. "
            f"Fix the code, not the law.{skip}",
            tone="fail",
        )
    if report.exit_code == exitcodes.WAITING:
        held = f"{c['holds']} of {ui.plural(total, 'law')} hold" if total else "There are no laws"
        return ui.banner(
            "WAITING FOR YOUR DECISION",
            f"{held}, but {ui.plural(n_dec, 'thing needs', 'things need')} a person's "
            f"decision first.{skip}",
            tone="wait",
        )
    if total == 0:
        return ui.banner(
            "ALL LAWS HOLD", "There are no laws yet, so nothing was checked.", tone="ok"
        )
    if report.only_laws:
        known = report.laws_known if report.laws_known is not None else total
        others = max(known - total, 0)
        return ui.banner(
            "ALL LAWS HOLD",
            f"The {ui.plural(total, 'law')} you named held. This was a partial run: "
            f"{ui.plural(others, 'other law was', 'other laws were')} not checked.{skip}",
            tone="ok",
        )
    every = "Your law" if total == 1 else f"All {total} of your laws"
    return ui.banner(
        "ALL LAWS HOLD",
        f"{every} held on your current code.{skip}",
        tone="ok",
    )


def _qualname(r: LawResult) -> str | None:
    if r.nodeid and "::" in r.nodeid:
        return r.nodeid.split("::", 1)[1].replace("::", ".").split("[", 1)[0]
    return None


def _failure_panel(report: CheckReport, r: LawResult, details: bool):
    from lawkeep import discovery, ui
    from lawkeep.explain import breaking_input_text, failure_lines, tidy
    from lawkeep.output import plain_example

    parts: list[RenderableType] = [ui.quote(r.sentence), ui.blank()]
    rows: list[tuple[str, str | Text | None]] = []
    if r.simplest_failing_example is not None:
        ex = r.simplest_failing_example
        text = (
            tidy(plain_example(ex)) if set(ex) == {"call"} else breaking_input_text(ex, raw=details)
        )
        rows.append(("The input that breaks it:", text))
    msg = failure_lines(r.failure_message, details=details)
    if msg:
        label = "Why it failed:" if r.status == "fails" else "What happened:"
        rows.append((label, "\n".join(msg)))
    if rows:
        parts.append(ui.label_value(rows))
    if r.status == "error" and not msg:
        parts.append(ui.status("fail", "The law could not run; run with --details for more."))
    qual = _qualname(r)
    if r.file and qual and r.simplest_failing_example and "call" not in r.simplest_failing_example:
        desc = discovery.describe_inputs_in_file(Path(report.root) / r.file, qual)
        names = [k for k in r.simplest_failing_example if k in desc]
        if names:
            from lawkeep.explain import INPUTS_TIP, is_made_up

            parts += [
                ui.blank(),
                ui.bold("What the inputs mean"),
                ui.indent(ui.label_value([(f"{k}:", desc[k]) for k in names])),
            ]
            if any(is_made_up(desc[k]) for k in names):
                parts.append(ui.dim(INPUTS_TIP))
    replayed = r.evidence.get("saved_failures_replayed")
    if replayed and details:
        parts.append(
            ui.dim(f"lawkeep also re-tried {ui.plural(replayed, 'saved failing example')}.")
        )
    title = "This law fails" if r.status == "fails" else "This law could not run"
    parts.append(ui.ident(f"law id {r.law_id}" + (f" · file {r.file}" if r.file else "")))
    return ui.panel(*parts, title=title, tone="fail")


def render(
    report: CheckReport,
    results_profile: str,
    *,
    details: bool = False,
    waiting_list: WaitingList | None = None,
) -> str:
    """The human screen for `lawkeep check` (plain or colour, fitted to the terminal)."""
    from lawkeep import ui

    out: list[RenderableType] = [_banner(report)]
    order = {"fails": 0, "error": 1, "skipped": 2, "not_run": 3, "holds": 4}
    laws = sorted(report.laws, key=lambda r: (order.get(r.status, 9), r.law_id))
    if laws:
        headers = ["Law", "Result"]
        rows: list[list[RenderableType]] = []
        for r in laws:
            word, tone = _STATUS_WORD.get(r.status, (r.status, "info"))
            name: RenderableType = Text(f"“{r.sentence}”" if r.sentence else r.law_id)
            if details:
                name = ui.group(name, ui.ident(r.law_id))
            cell = Text(word, style={"ok": "green", "fail": "bold red", "warn": "yellow"}[tone])
            row: list[RenderableType] = [name, cell]
            if details:
                tried = f"{r.examples_tried}"
                if r.evidence.get("backend") == "crosshair":
                    tried += " (all paths)" if r.evidence.get("exhausted") else " (not all paths)"
                row += [tried, f"{r.duration_seconds:.2f}s"]
            rows.append(row)
        if details:
            headers += ["Tried", "Time"]
        out += [ui.blank(), ui.table(headers, rows, ratios=[6, 1] + ([1, 1] if details else []))]
    for r in laws:
        thrown = r.evidence.get("inputs_thrown_away") or 0
        if thrown and thrown >= max(r.examples_tried, 1):
            out.append(
                ui.warning(
                    f"“{r.sentence or r.law_id}”: lawkeep had to throw away {thrown} generated "
                    f"inputs and kept only {r.examples_tried}, so this law checks fewer cases "
                    "than it seems."
                )
            )
    for r in laws:
        if r.status in ("fails", "error"):
            out += [ui.blank(), _failure_panel(report, r, details)]
        elif r.status == "skipped" and r.failure_message:
            out.append(
                ui.warning(
                    f"“{r.sentence or r.law_id}” was skipped: "
                    f"{r.failure_message.strip().splitlines()[0]}"
                )
            )

    errors = [i for i in report.integrity if i.severity == "error"]
    others = [i for i in report.integrity if i.severity != "error"]
    if errors or report.waiting:
        out += [ui.blank(), ui.heading("Needs a person's decision")]
        for i in errors:
            out.append(ui.warning(i.message))
        for w in report.waiting:
            what = f"“{w.sentence}”" if w.sentence else (w.law_id or "a law")
            word = {"looser": "a looser", "stricter": "a stricter", "same": "a"}.get(
                w.direction or "", "a"
            )
            ref = w.history_id or w.law_id or ""
            out.append(
                ui.warning(
                    f"{word.capitalize()} change to {what} is waiting for your yes. "
                    f"See it with `lawkeep approve {ref}`.".replace("  ", " ")
                )
            )
    notes = list(report.notes)
    if notes:
        out.append(ui.blank())
        out += [ui.status("info", n) for n in notes]
    if others:
        if details:
            out += [ui.blank(), ui.heading("Worth a look (does not change the result)")]
            for i in others:
                where = f" ({i.path})" if i.path and i.path not in i.message else ""
                out.append(ui.status("info", f"{i.message}{where}"))
        else:
            out.append(
                ui.status(
                    "info",
                    f"{ui.plural(len(others), 'note')} about your project worth a look (they do "
                    "not change the result) — run `lawkeep check --details` to see them.",
                )
            )
    line = waiting_list.line() if waiting_list is not None else None
    if line:
        out += [ui.blank(), ui.status("warn", line)]
    out += [ui.blank(), ui.dim(_footer(report, results_profile))]
    step = _next_step(report)
    if step:
        out.append(ui.next_step(step[:1].upper() + step[1:]))
    return ui.render(*out)


def _footer(report: CheckReport, results_profile: str) -> str:
    return f"{results_profile} · took {report.duration_seconds:.1f}s"


def run(args: argparse.Namespace) -> int:
    root = paths.require_root(args.root)
    cfg = config.load_config(root)
    started = utc_now_iso()
    t0 = time.monotonic()
    profile = cfg.check.get_profile(args.profile)
    if getattr(args, "deep_check", False):
        profile = dataclasses.replace(profile, backend="crosshair")

    issues = integrity.check_integrity(root, cfg, base_ref=getattr(args, "base", None))
    outcome = runner.run_laws(
        root, cfg, profile=profile, law_ids=args.law, timeout=getattr(args, "timeout", None)
    )
    notes = [f"lawkeep.toml: {w}" for w in cfg.warnings]
    notes += outcome.notes
    notes += outcome.collection_errors
    if outcome.new_failures:
        if getattr(args, "no_save", False):
            notes.append(
                f"{len(outcome.new_failures)} new failing input(s) were found but not saved "
                f"(--no-save)."
            )
        else:
            _, save_notes = _save_new_failures(root, outcome, profile.name)
            notes += save_notes
    if profile.backend == "crosshair":
        notes.append(
            "CrossHair is experimental. A law marked 'not all paths' held for the paths through "
            "the code that CrossHair tried, not necessarily for every input."
        )

    only = list(dict.fromkeys(args.law or []))
    known = None
    if only:
        from lawkeep import discovery

        known = len(discovery.discover_laws(root).laws)
        notes.append(
            f"Partial run: only {len(only)} of {known} law(s) were checked (--law). The other "
            f"laws were not run, so this result says nothing about them. Run `lawkeep check` "
            f"without --law before saying all laws hold."
        )
    report = CheckReport(
        profile=profile.name,
        root=str(root),
        laws=outcome.results,
        integrity=issues,
        waiting=_pending(root),
        notes=notes,
        started_at=started,
        only_laws=only,
        laws_known=known,
    )
    from lawkeep import presence, waiting

    wl = waiting.collect(root, issues=issues)
    report.waiting_for_person = wl.to_dict()
    seen = presence.detect()
    report.person_present, report.person_present_reason = seen.present, seen.reason
    report.duration_seconds = round(time.monotonic() - t0, 3)
    report.finalize()
    report.summary = _summary(report)
    text = None
    if not args.json:
        text = render(
            report,
            _profile_text(report, outcome.results),
            details=bool(getattr(args, "details", False)),
            waiting_list=wl,
        )
    output.emit(report, args.json, text=text)
    return report.exit_code
