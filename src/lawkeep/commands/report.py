"""`lawkeep report`: show law changes and what your laws do not protect.

Owner: workstream C (strength & protection).

`lawkeep report [--since ISO | --session] [--base REF] [--no-protection] [--format text|markdown]`

- **Protection report** ("only review what your laws don't protect"): changed functions since
  the base (default: merge-base with main/master), each labelled protected / partly / NOT
  protected by planting bugs only in them (``lawkeep.protection``).
- **Law change report**: every law change since ``--since`` (default: the start of the current
  agent session if one was recorded, else all history), pending decisions, and integrity issues
  (laws changed outside lawkeep, side doors). Text comes from ``lawchanges.render_since``.

``--format markdown`` is what the GitHub Action posts as a pull-request comment.
Result codes: 0 nothing waiting · 2 a person's decision is needed (pending law changes or
integrity errors) · 3 setup problem. The protection part never changes the code by itself:
lawkeep shows; people decide.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import RenderableType

from lawkeep import config, exitcodes, history, integrity, lawchanges, output, paths, ui
from lawkeep import protection as protection_mod
from lawkeep.errors import HistoryError, SetupError
from lawkeep.models import IntegrityIssue, LawChange, LawChangeReport, ProtectionReport, parse_iso
from lawkeep.strength import render
from lawkeep.waiting import WaitingList

HELP = "show every law change, anything waiting for you, and which changed code no law protects"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    when = parser.add_mutually_exclusive_group()
    when.add_argument(
        "--since",
        metavar="TIMESTAMP",
        default=None,
        help="show law changes at or after this time (ISO 8601, e.g. 2026-11-21T10:00:00Z)",
    )
    when.add_argument(
        "--session",
        action="store_true",
        help="show law changes since the current agent session started (the default when a "
        "session start was recorded)",
    )
    when.add_argument("--all", action="store_true", help="show every recorded law change")
    parser.add_argument(
        "--base",
        metavar="REF",
        default=None,
        help="compare code changes with this branch, tag or commit (default: the merge-base "
        "with main or master, plus uncommitted changes)",
    )
    parser.add_argument(
        "--no-protection",
        action="store_true",
        help="skip the check of which changed code your laws protect (it plants small bugs "
        "in a copy of the code, which takes a while)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="text for the terminal, markdown for a pull-request comment (not used with --json)",
    )


def _since(args: argparse.Namespace, root: Path, notes: list[str]) -> str | None:
    if args.since:
        try:
            parse_iso(args.since)
        except ValueError:
            raise SetupError(
                f"--since {args.since!r} is not a date and time lawkeep can read.",
                hint="Use ISO 8601, e.g. --since 2026-11-21T10:00:00Z",
            ) from None
        return args.since
    if getattr(args, "all", False):
        return None
    start = history.session_start(root)
    if start:
        return start
    if args.session:
        notes.append("No agent session start was recorded, so every law change is shown.")
    return None


def _changes(
    root: Path, since: str | None, notes: list[str]
) -> tuple[list[LawChange], list[LawChange], set[str]]:
    """(changes since `since`, pending decisions not already in that list, pending ids)."""
    try:
        entries = history.read(root)
        changes = lawchanges.changes_since(root, since)
    except HistoryError as exc:
        notes.append(f"The law history could not be read: {exc.message}")
        return [], [], set()
    pending_entries = history.pending(entries)
    seen = {c.history_id for c in changes if c.history_id}
    extra = [c for c in lawchanges.list_pending(root) if c.history_id not in seen]
    return changes, extra, {e.id for e in pending_entries}


def _integrity(root: Path, cfg: config.Config, base: str | None) -> list[IntegrityIssue]:
    return list(integrity.check_integrity(root, cfg, base_ref=base))


def run(args: argparse.Namespace) -> int:
    root = paths.require_root(args.root)
    cfg = config.load_config(root)
    notes: list[str] = list(cfg.warnings)
    since = _since(args, root, notes)
    changes, pending_extra, pending_ids = _changes(root, since, notes)
    integrity_issues = _integrity(root, cfg, args.base)

    protection: ProtectionReport | None = None
    if not args.no_protection:
        try:
            protection = protection_mod.protection_report(root, cfg, base_ref=args.base)
        except SetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - the law change report must still be shown
            notes.append(f"The protection report could not run ({type(exc).__name__}: {exc}).")

    from lawkeep import presence, waiting

    wl = waiting.collect(root, issues=integrity_issues)
    seen = presence.detect()
    report = LawChangeReport(
        since=since,
        changes=changes + pending_extra,
        integrity=integrity_issues,
        protection=protection,
        notes=notes,
        waiting_for_person=wl.to_dict(),
        person_present=seen.present,
        person_present_reason=seen.reason,
    )
    # a waiting entry decided later is not open: only count what history.pending says
    open_decisions = [
        c for c in report.changes if c.change == "waiting" and c.history_id in pending_ids
    ]
    errors = [i for i in integrity_issues if i.severity == "error"]
    codes = [exitcodes.ALL_HOLD]
    if open_decisions or errors:
        codes.append(exitcodes.WAITING)
    report.exit_code = exitcodes.combine(*codes)

    applied = [c for c in report.changes if c.change not in ("waiting", "rejected")]
    parts = [f"{len(applied)} law change(s) {'since ' + since if since else 'recorded'}"]
    if open_decisions:
        parts.append(f"{len(open_decisions)} waiting for your decision")
    if errors:
        parts.append(f"{len(errors)} integrity problem(s) need a person's decision")
    if protection is not None and protection.functions:
        need = sum(1 for f in protection.functions if f.status != "protected")
        parts.append(
            f"{need} of {len(protection.functions)} changed function(s) not fully protected"
        )
    report.summary = "; ".join(parts) + "."

    if args.json:
        output.emit(report, True)
        return report.exit_code
    if args.format == "markdown":
        text = _markdown(root, report, wl)
    else:
        text = _text(root, report, wl)
    output.emit(report, False, text=text)
    return report.exit_code


def _verdict(report: LawChangeReport) -> tuple[str, str, str]:
    """(title, one plain sentence, tone) for the top of the report."""
    applied = [c for c in report.changes if c.change not in ("waiting", "rejected")]
    waiting = [c for c in report.changes if c.change == "waiting"]
    errors = [i for i in report.integrity if i.severity == "error"]
    since = f" since {report.since}" if report.since else ""
    bits = [f"{len(applied)} law change(s) recorded{since}."]
    unprotected = []
    if report.protection is not None and report.protection.functions:
        unprotected = [f for f in report.protection.functions if f.status != "protected"]
        bits.append(
            f"{len(unprotected)} of {len(report.protection.functions)} changed function(s) "
            "are not fully protected by laws."
        )
    if report.exit_code == exitcodes.WAITING:
        n = len(waiting) + len(errors)
        return (
            "WAITING FOR YOUR DECISION",
            f"{ui.plural(n, 'item needs', 'items need')} a person's decision. " + " ".join(bits),
            "wait",
        )
    if unprotected:
        return "SOME CHANGED CODE IS NOT PROTECTED", " ".join(bits), "warn"
    return "NOTHING NEEDS YOUR DECISION", " ".join(bits), "ok"


def _next(report: LawChangeReport) -> str:
    if report.exit_code == exitcodes.WAITING or report.waiting_for_person.get("count"):
        return "decide the waiting items with `lawkeep approve` (it shows each one)."
    if report.protection is not None and any(
        f.status != "protected" for f in report.protection.functions
    ):
        return (
            "read the code marked “Not protected” yourself, or ask your coding agent to draft "
            "laws for it (`lawkeep draft`)."
        )
    return "nothing to do."


def _text(root: Path, report: LawChangeReport, wl: WaitingList | None = None) -> str:
    title, detail, tone = _verdict(report)
    out: list[RenderableType] = [ui.banner(title, detail, tone=tone)]
    if report.protection is not None:
        out += [ui.blank(), render.protection_view(report.protection)]
    view, _ = lawchanges.changes_view(
        root, report.since, integrity=report.integrity, waiting_list=wl
    )
    out += [ui.blank(), view]
    if report.notes:
        out.append(ui.blank())
        out += [ui.status("info", n) for n in report.notes]
    out += [ui.blank(), ui.next_step(_next(report))]
    return ui.render(*out)


def _markdown(root: Path, report: LawChangeReport, wl: WaitingList | None = None) -> str:
    title, detail, _ = _verdict(report)
    out = ["## lawkeep report", "", f"**{title}**: {detail}", ""]
    if report.protection is not None:
        out.append(render.protection_markdown(report.protection))
    text = lawchanges.render_since(
        root, report.since, integrity=report.integrity, as_markdown=True, waiting_list=wl
    )
    out += [text.rstrip(), ""]
    for n in report.notes:
        out.append(f"- note: {n}")
    out += [
        "",
        f"**Next step:** {_next(report)}",
        "",
        "_lawkeep shows and records; it does not block merges by itself._",
    ]
    return "\n".join(out).rstrip() + "\n"
