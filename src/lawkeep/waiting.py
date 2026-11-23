"""One waiting list: everything that waits for a person's decision, counted in one place.

Five kinds (the order people see them in):

- ``draft``: a new law a coding agent drafted (``laws/drafts/*.py``), one item per draft file;
- ``proposal``: a proposed change to a law that waits for a person's yes (looser, unclear,
  a deletion, a skip, or a new law that is not strong enough to go in by itself);
- ``draft_edit``: a change the drafting AI made to an approved file during ``lawkeep draft``
  (``draft_edits``), parked for review;
- ``unrecorded``: law files changed outside lawkeep (one item for all of them);
- ``unconfirmed``: an approval recorded without a terminal (``approvals``), to confirm or undo.

:func:`collect` is static and cheap (no project code runs, no strength checks) and never
raises, so ``check``, ``report``, the session hooks and the MCP server can all show the same
count: "3 things wait for your decision — run `lawkeep approve`". ``lawkeep approve`` shows
the same items in full and lets a person decide them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lawkeep.models import HistoryEntry, IntegrityIssue, as_list

KINDS = ("draft", "proposal", "draft_edit", "unrecorded", "unconfirmed")

#: (one, many) in plain words, for counts.
LABELS: dict[str, tuple[str, str]] = {
    "draft": ("new draft", "new drafts"),
    "proposal": ("proposed law change", "proposed law changes"),
    "draft_edit": ("change by the drafting AI", "changes by the drafting AI"),
    "unrecorded": ("law change made outside lawkeep", "law changes made outside lawkeep"),
    "unconfirmed": ("approval made without a terminal", "approvals made without a terminal"),
}

#: What each kind means and what lawkeep suggests, for the approve screen.
EXPLAIN: dict[str, tuple[str, str]] = {
    "draft": (
        "New laws a coding agent wrote. lawkeep does not check them until you approve them.",
        "Approve the ones marked “Recommended”; read the others.",
    ),
    "proposal": (
        "Changes to approved laws that wait for your yes (looser, unclear, or a deletion).",
        "Say yes only if you asked for the change or agree with the reason.",
    ),
    "draft_edit": (
        "Files the drafting AI changed although it was asked to write drafts only. The "
        "approved versions are still in place.",
        "Usually reject; say yes only if you asked for that change.",
    ),
    "unrecorded": (
        "Law files someone changed directly, without lawkeep. lawkeep cannot tell who did it.",
        "Accept only if you know about the change; otherwise put the files back with git.",
    ),
    "unconfirmed": (
        "Approvals a coding agent recorded on someone's behalf, without a terminal. lawkeep "
        "cannot tell whether a person really said yes.",
        "Confirm the ones you remember approving; undo the others.",
    ),
}


@dataclass
class WaitingItem:
    kind: str  # one of KINDS
    ref: str  # what to type after `lawkeep approve`
    title: str  # one plain line
    ids: list[str] = field(default_factory=list[str])  # history ids or file paths

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "title": self.title, "ids": self.ids}


@dataclass
class WaitingList:
    items: list[WaitingItem] = field(default_factory=list["WaitingItem"])
    problems: list[str] = field(default_factory=list[str])

    @property
    def count(self) -> int:
        return len(self.items)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in KINDS}
        for it in self.items:
            out[it.kind] = out.get(it.kind, 0) + 1
        return out

    def breakdown(self) -> str:
        """``2 new drafts, 1 proposed law change``."""
        parts: list[str] = []
        for kind, n in self.counts().items():
            if n:
                one, many = LABELS[kind]
                parts.append(f"{n} {one if n == 1 else many}")
        return ", ".join(parts)

    def line(self) -> str | None:
        """The one-line count people see (None when nothing waits)."""
        if not self.items:
            return None
        n = self.count
        verb = "thing waits" if n == 1 else "things wait"
        return f"{n} {verb} for your decision ({self.breakdown()}) — run `lawkeep approve`."

    def to_dict(self) -> dict[str, Any]:
        """The additive JSON field ``waiting_for_person``."""
        return {
            "count": self.count,
            "ids": [it.ref for it in self.items],
            "by_kind": self.counts(),
            "items": [it.to_dict() for it in self.items],
            "command": "lawkeep approve",
        }


def unconfirmed_ids_from(issues: list[IntegrityIssue] | None) -> list[str]:
    """History ids the integrity check flagged as ``approval_unconfirmed``."""
    out: list[str] = []
    for i in issues or []:
        if i.kind == "approval_unconfirmed":
            out += [str(x) for x in as_list(i.details.get("entries")) if x]
    return out


def collect(root: Path, *, issues: list[IntegrityIssue] | None = None) -> WaitingList:
    """Everything waiting for a person, without running any project code. Never raises."""
    from lawkeep import approvals, history, lock

    out = WaitingList()
    root = Path(root)
    try:
        from lawkeep import discovery
        from lawkeep.commands.draft import draft_files
        from lawkeep.paths import rel

        for f in draft_files(root):
            r = rel(root, f)
            laws = discovery.laws_in_file(f, root).laws
            first = next((i.sentence for i in laws if i.sentence), None)
            title = f"new draft “{first}”" if first else f"new draft {r}"
            if len(laws) > 1:
                title += f" (+{len(laws) - 1} more)"
            out.items.append(WaitingItem("draft", r, title, [r]))
    except Exception as exc:  # noqa: BLE001 - a count must never break the caller
        out.problems.append(f"could not list drafts ({type(exc).__name__}: {exc})")
    entries: list[HistoryEntry] = []
    try:
        entries = history.read(root)
        for e in history.pending(entries):
            what = e.law_id or e.details.get("target_file") or "a law file"
            if e.details.get("from_draft"):
                out.items.append(
                    WaitingItem("draft_edit", e.id, f"the drafting AI changed {what}", [e.id])
                )
            else:
                word = {"looser": "looser", "unclear": "unclear"}.get(e.direction or "", "")
                if e.action == "remove_law":
                    title = f"deleting {what}"
                elif e.action == "skip_law":
                    title = f"skipping {what}"
                else:
                    title = f"{word + ' ' if word else ''}change to {what}"
                out.items.append(WaitingItem("proposal", e.id, title, [e.id]))
    except Exception as exc:  # noqa: BLE001
        out.problems.append(f"could not read the law history ({type(exc).__name__}: {exc})")
    try:
        changed = list(lock.diff_against_lock(root).paths())
        try:
            more = history.unexplained_lock_paths(lock.read_lock(root), entries)
        except Exception:  # noqa: BLE001
            more = []
        changed += [p for p in more if p not in changed]
        if changed:
            out.items.append(
                WaitingItem(
                    "unrecorded",
                    "unrecorded",
                    f"{len(changed)} law file(s) changed outside lawkeep",
                    changed,
                )
            )
    except Exception as exc:  # noqa: BLE001
        out.problems.append(f"could not compare with laws.lock ({type(exc).__name__}: {exc})")
    try:
        for g in approvals.unconfirmed_groups(entries, unconfirmed_ids_from(issues)):
            what = ", ".join(g.law_ids) or str(g.entries[0].action)
            out.items.append(
                WaitingItem(
                    "unconfirmed", g.id, f"approval of {what} made without a terminal", g.ids
                )
            )
    except Exception as exc:  # noqa: BLE001
        out.problems.append(f"could not list approvals ({type(exc).__name__}: {exc})")
    return out
