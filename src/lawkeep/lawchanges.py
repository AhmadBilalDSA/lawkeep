"""The law change report: every law change since a point in time, in plain English.

Owner: workstream D (law lifecycle). Called by ``report`` (C), ``hook session-end`` (E) and
the MCP server (E).

Example (text, plain mode)::

    Law changes in this session
    ---------------------------
    What happened     Law                                Who, why and evidence
    ---------------------------------------------------------------------------
    Added             “A refund is never negative”       by coding agent (claude-code)
                                                         approved by Ada
                                                         stricter
    Waiting for you   “Fee is always 2%” → “Fee is 2%,   Looser: needs your yes
                      or $0 under $1”                    why: no fees on tiny refunds
                                                         now allowed: amount=0.50 → ...

The words come from docs/OUTPUT-STYLE.md (``WHAT``); Markdown uses the same words.

A waiting entry that was decided later is shown once, as its decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from lawkeep import history
from lawkeep.models import (
    ExampleRow,
    HistoryEntry,
    IntegrityIssue,
    LawChange,
    LawChangeReport,
    ProtectionReport,
    as_dict,
    as_list,
)
from lawkeep.waiting import WaitingList

if TYPE_CHECKING:
    from rich.console import RenderableType


def _examples(entry: HistoryEntry) -> list[ExampleRow]:
    out: list[ExampleRow] = []
    for item in as_list(as_dict(entry.details).get("examples")):
        if isinstance(item, dict):
            try:
                out.append(ExampleRow.from_dict(cast("dict[str, Any]", item)))
            except (TypeError, ValueError):
                continue
    return out


def _to_change(entry: HistoryEntry) -> LawChange:
    change = LawChange.from_history(entry)
    change.examples = _examples(entry)
    if change.evidence is None:
        summary: object = as_dict(as_dict(entry.details).get("evidence")).get("summary")
        if isinstance(summary, str):
            change.evidence = summary
    return change


def changes_since(root: Path, since: str | None) -> list[LawChange]:
    """Every recorded law change at or after ``since`` (``None`` = all), oldest first."""
    all_entries = history.read(root)
    decided = {e.refers_to for e in all_entries if e.refers_to}
    entries = history.since(root, since) if since else all_entries
    out: list[LawChange] = []
    for e in entries:
        if e.status == "waiting" and e.id in decided:
            continue  # shown as its decision
        out.append(_to_change(e))
    return out


def list_pending(root: Path) -> list[LawChange]:
    """Changes waiting for a person's decision (from ``history.pending``)."""
    return [_to_change(e) for e in history.pending(history.read(root))]


def _integrity(
    root: Path, integrity: list[IntegrityIssue] | None
) -> tuple[list[IntegrityIssue], list[str]]:
    if integrity is not None:
        return integrity, []
    try:
        from lawkeep import config
        from lawkeep import integrity as integrity_mod

        return integrity_mod.check_integrity(root, config.load_config(root)), []
    except NotImplementedError:
        return [], ["The unrecorded-change check is not available yet."]
    except Exception as exc:  # noqa: BLE001 - the report must still show the history
        return [], [f"The unrecorded-change check could not run ({type(exc).__name__}: {exc})."]


#: What happened, in the words people see (text and Markdown alike).
WHAT = {
    "added": "Added",
    "updated": "Changed",
    "removed": "Removed",
    "waiting": "Waiting for you",
    "rejected": "Rejected",
    "unrecorded": "Accepted by hand",  # a person accepted a change made outside lawkeep
    "confirmed": "Approval confirmed",  # a person confirmed an approval made without a terminal
    "undone": "Approval undone",  # a person undid an approval made without a terminal
}
_DIRECTION_WORDS = {
    "stricter": "stricter",
    "same": "same meaning",
    "looser": "looser",
    "unclear": "unclear whether stricter or looser",
}


def who(actor: str | None) -> str:
    """``agent:claude-code`` -> ``coding agent (claude-code)``; ``person:Ada`` -> ``Ada``."""
    if not actor:
        return "someone"
    if actor.startswith("agent:"):
        return f"coding agent ({actor.split(':', 1)[1]})"
    if actor.startswith("person:"):
        return actor.split(":", 1)[1]
    return actor


def _quote(s: str | None) -> str:
    return f"“{s}”" if s else ""


def _law_cell(c: LawChange) -> str:
    if (
        c.change in ("waiting", "updated")
        and c.sentence_before
        and c.sentence
        and c.sentence != c.sentence_before
    ):
        return f"{_quote(c.sentence_before)} → {_quote(c.sentence)}"
    return _quote(c.sentence) or (c.law_id or "")


def _details(c: LawChange) -> list[str]:
    """Who did it, why, and the evidence, in short plain phrases."""
    out: list[str] = []
    direction = _DIRECTION_WORDS.get(c.direction or "", "")
    if c.change in ("confirmed", "undone"):
        verb = "confirmed" if c.change == "confirmed" else "undone"
        out.append(f"{verb} by {who(c.approved_by or c.actor)} in a terminal")
        if c.reason:
            out.append(f"why: {c.reason}")
        return out
    if c.change == "waiting":
        word = (direction or "unclear").capitalize()
        out.append(f"{word}: needs your yes")
        if c.actor:
            out.append(f"asked by {who(c.actor)}")
    else:
        if c.change == "rejected":
            out.append(f"rejected by {who(c.approved_by or c.actor)}")
        else:
            if c.actor and c.actor.startswith("agent:"):
                out.append(f"by {who(c.actor)}")
            if c.approved_by:
                out.append(f"approved by {who(c.approved_by)}")
            elif c.permission_used == "auto":
                out.append("went in without asking (your settings allow it)")
        if direction and c.change not in ("added", "rejected"):
            out.append(
                f"makes the law {direction}" if c.direction in ("stricter", "looser") else direction
            )
        if c.evidence:
            out.append(c.evidence)
    if c.reason:
        out.append(f"why: {c.reason}")
    if c.change == "waiting":
        if c.evidence:
            out.append(f"evidence: {c.evidence}")
        for ex in c.examples[:2]:
            inputs = ", ".join(f"{k}={v}" for k, v in ex.inputs.items()) or "(no inputs)"
            kind = (
                "now allowed"
                if ex.old_holds is False and ex.holds
                else ("now rejected" if ex.old_holds and ex.holds is False else "example")
            )
            res = f" → {ex.result}" if ex.result else ""
            out.append(f"{kind}: {inputs}{res}")
    return out


def _md(text: str) -> str:
    """Make free text safe inside a Markdown table cell (no stray code spans, HTML or pipes)."""
    return text.replace("`", "'").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|")


def _issue_text(issue: IntegrityIssue) -> str:
    where = f" ({issue.path})" if issue.path and issue.path not in issue.message else ""
    return f"{issue.message}{where}"


def _title(root: Path, since: str | None) -> str:
    session = history.session_start(root)
    if since and session and since == session:
        return "Law changes in this session"
    if since:
        return f"Law changes since {since}"
    return "All recorded law changes"


def changes_view(
    root: Path,
    since: str | None,
    *,
    integrity: list[IntegrityIssue] | None = None,
    protection: ProtectionReport | None = None,
    waiting_list: WaitingList | None = None,
) -> tuple[RenderableType, int]:
    """The law change report as Rich renderables (see :func:`render_since`)."""
    from rich.text import Text

    from lawkeep import ui

    root = Path(root)
    changes = changes_since(root, since)
    issues, notes = _integrity(root, integrity)
    out: list[RenderableType] = [ui.heading(_title(root, since))]
    rows: list[list[str | Text]] = []
    for c in changes:
        if c.change == "added" and not c.law_id:
            rows.append([Text("Set up"), "lawkeep was set up in this project", ""])
            continue
        style = {"waiting": "bold yellow", "rejected": "red", "unrecorded": "yellow"}.get(
            c.change, ""
        )
        details = Text("\n".join(_details(c)))
        rows.append([Text(WHAT.get(c.change, c.change), style=style), _law_cell(c), details])
    if rows:
        out.append(
            ui.table(["What happened", "Law", "Who, why and evidence"], rows, ratios=[2, 4, 6])
        )
    else:
        out.append(ui.status("info", "No law changes."))
    shown = [i for i in issues if i.severity != "info"]
    if shown:
        out += [
            ui.blank(),
            ui.heading("Flagged: law files changed outside lawkeep, and other risks"),
        ]
        out += [ui.warning(_issue_text(i)) for i in shown]
    if protection is not None:
        counts = {"protected": 0, "partly": 0, "not_protected": 0}
        for f in protection.functions:
            counts[f.status] = counts.get(f.status, 0) + 1
        out.append(
            ui.status(
                "info",
                f"Changed code: {counts['protected']} function(s) protected by laws, "
                f"{counts['partly']} partly, {counts['not_protected']} not protected.",
            )
        )
        for f in protection.functions:
            if f.status != "protected":
                out.append(
                    ui.indent(
                        ui.dim(
                            f"{f.function} ({f.file}): {f.status.replace('_', ' ')}, "
                            f"{f.planted_bugs_caught} of {f.planted_bugs_total} planted bugs caught"
                        )
                    )
                )
    pending = [c for c in changes if c.change == "waiting"]
    errors = [i for i in issues if i.severity == "error"]
    final = _decision_line(pending, errors, waiting_list)
    if final:
        out += [ui.blank(), ui.warning(final)]
    out += [ui.status("info", n) for n in notes]
    return ui.group(*out), len(pending) + len(errors)


_UNRECORDED_KINDS = (
    "unrecorded_law_change",
    "unrecorded_law_added",
    "unrecorded_law_removed",
    "lock_rewritten",
)


def _decision_line(
    pending: list[LawChange], errors: list[IntegrityIssue], waiting_list: WaitingList | None
) -> str | None:
    """The closing line about decisions. With the one waiting list (``waiting.collect``) it is
    its count ("3 things wait for your decision (...) — run `lawkeep approve`."), plus any
    flagged risk the list does not cover; without it, the older count of pending changes and
    integrity errors."""
    if waiting_list is None:
        if not (pending or errors):
            return None
        return (
            f"{len(pending) + len(errors)} item(s) need a person's decision: run "
            "`lawkeep approve` (or ask your coding agent to show them)."
        )
    others = [i for i in errors if i.kind not in _UNRECORDED_KINDS]
    line = waiting_list.line() or ""
    if others:
        line += (" " if line else "") + (
            f"{len(others)} flagged risk(s) above need a person's look "
            "(run `lawkeep check --details`)."
        )
    return line or None


def _markdown(
    root: Path,
    since: str | None,
    issues: list[IntegrityIssue],
    notes: list[str],
    protection: ProtectionReport | None,
    waiting_list: WaitingList | None = None,
) -> str:
    changes = changes_since(root, since)
    out = [f"### {_title(root, since)}", ""]
    rows: list[str] = []
    for c in changes:
        if c.change == "added" and not c.law_id:
            rows.append("| Set up | lawkeep was set up in this project | |")
            continue
        what = WHAT.get(c.change, c.change)
        if c.change == "waiting":
            what = f"**{what}**"
        details = "<br>".join(_md(d) for d in _details(c))
        law = _md(_law_cell(c))
        law_id = f"<br>`{c.law_id}`" if c.law_id else ""
        rows.append(f"| {what} | {law}{law_id} | {details} |")
    if rows:
        out += ["| What happened | Law | Who, why and evidence |", "| --- | --- | --- |", *rows]
    else:
        out.append("No law changes.")
    shown = [i for i in issues if i.severity != "info"]
    if shown:
        out += ["", "**Flagged** (law files changed outside lawkeep, and other risks):", ""]
        out += [f"- {_md(_issue_text(i))}" for i in shown]
    if protection is not None:
        counts = {"protected": 0, "partly": 0, "not_protected": 0}
        for f in protection.functions:
            counts[f.status] = counts.get(f.status, 0) + 1
        out += [
            "",
            f"- Changed code: {counts['protected']} function(s) protected by laws, "
            f"{counts['partly']} partly, {counts['not_protected']} not protected",
        ]
    pending = [c for c in changes if c.change == "waiting"]
    errors = [i for i in issues if i.severity == "error"]
    final = _decision_line(pending, errors, waiting_list)
    if final:
        out += ["", f"**{final}**"]
    for n in notes:
        out.append(f"- note: {_md(n)}")
    return "\n".join(out)


def render_since(
    root: Path,
    since: str | None,
    *,
    integrity: list[IntegrityIssue] | None = None,
    protection: ProtectionReport | None = None,
    as_markdown: bool = False,
    waiting_list: WaitingList | None = None,
) -> str:
    """The law change report ("Law changes in this session ..."), plain English.

    ``integrity=None`` runs the integrity check (workstream B) when it is available.
    ``as_markdown=True`` gives a version for pull-request comments (no Rich).
    """
    root = Path(root)
    if as_markdown:
        issues, notes = _integrity(root, integrity)
        return _markdown(root, since, issues, notes, protection, waiting_list)
    from lawkeep import ui

    view, _ = changes_view(
        root, since, integrity=integrity, protection=protection, waiting_list=waiting_list
    )
    return ui.render(view).rstrip("\n")


def build_report(
    root: Path,
    since: str | None,
    *,
    integrity: list[IntegrityIssue] | None = None,
    protection: ProtectionReport | None = None,
) -> LawChangeReport:
    """Structured version of :func:`render_since` (``LawChangeReport``), for ``--json``."""
    from lawkeep import exitcodes

    issues, notes = _integrity(Path(root), integrity)
    changes = changes_since(Path(root), since)
    waiting = [c for c in changes if c.change == "waiting"]
    errors = [i for i in issues if i.severity == "error"]
    code = exitcodes.WAITING if (waiting or errors) else exitcodes.ALL_HOLD
    summary = f"{len(changes)} law change(s)" + (f" since {since}" if since else "")
    if waiting or errors:
        summary += f"; {len(waiting) + len(errors)} item(s) need a person's decision"
    return LawChangeReport(
        exit_code=code,
        summary=summary + ".",
        since=since,
        changes=changes,
        integrity=list(issues),
        protection=protection,
        notes=notes,
    )


def to_jsonable(changes: list[LawChange]) -> list[dict[str, Any]]:
    return [c.to_dict() for c in changes]
