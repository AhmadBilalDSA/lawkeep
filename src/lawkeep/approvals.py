"""Approvals recorded without a terminal: find them, and let a person confirm or undo them.

``lawkeep approve ID --yes`` run by a coding agent (after the person's yes in the agent's own
permission prompt) records ``details.terminal: false``: lawkeep records who *claimed* to
approve, it cannot confirm that a person did. Such approvals are "unconfirmed" until a person
in a terminal either

- **confirms** them: a ``confirm_approval`` history entry (``details.entries`` lists the
  approval ids, ``details.terminal: true``); nothing on disk changes; or
- **undoes** them: the law files go back to what they were before the approval and an
  ``undo_approval`` entry is recorded (through ``history.record_change``, so ``laws.lock`` is
  refreshed). An undone draft approval puts the law file back in ``laws/drafts/`` so it waits
  again.

To undo safely lawkeep needs the earlier bytes of every file the approval changed. When an
approval is recorded without a terminal, ``keep_copies`` saves them under
``laws/drafts/proposals/undo/<history id>/`` (ignored by the gate and the lock, protected by
the guard like pending proposals). The append-only history says what each file's fingerprint
was before the approval, so a copy that does not match is never used. Without a usable copy
lawkeep looks for the earlier version in git; if it cannot find it, it refuses and says so.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lawkeep import history, lock, paths
from lawkeep.models import HistoryEntry, as_dict, as_list

CONFIRM = "confirm_approval"
UNDO = "undo_approval"
MANIFEST = "manifest.json"


def undo_root(root: Path) -> Path:
    return paths.drafts_dir(root) / "proposals" / "undo"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------
# Which approvals are unconfirmed


def settled_ids(entries: Iterable[HistoryEntry]) -> set[str]:
    """Ids of approvals a person already confirmed or undid."""
    out: set[str] = set()
    for e in entries:
        if e.action in (CONFIRM, UNDO):
            out.update(str(x) for x in as_list(e.details.get("entries")))
    return out


def recorded_without_terminal(e: HistoryEntry) -> bool:
    """An applied change whose person's yes came without a terminal (``terminal: false``)."""
    return (
        e.status == "applied"
        and str(e.approved_by or "").startswith("person:")
        and e.details.get("terminal") is False
        and e.action not in (CONFIRM, UNDO)
    )


@dataclass
class UnconfirmedGroup:
    """One approval to confirm or undo (several entries when one draft held several laws)."""

    entries: list[HistoryEntry] = field(default_factory=list["HistoryEntry"])

    @property
    def id(self) -> str:
        return self.entries[0].id

    @property
    def ids(self) -> list[str]:
        return [e.id for e in self.entries]

    @property
    def law_ids(self) -> list[str]:
        return [e.law_id for e in self.entries if e.law_id]

    def to_dict(self) -> dict[str, Any]:
        first = self.entries[0]
        return {
            "history_ids": self.ids,
            "law_ids": self.law_ids,
            "action": first.action,
            "direction": first.direction,
            "approved_by": first.approved_by,
            "channel": first.details.get("channel"),
            "sentences": [e.sentence_after or e.sentence_before for e in self.entries],
            "timestamp": first.timestamp,
        }


def unconfirmed_groups(
    entries: list[HistoryEntry], also: Iterable[str] = ()
) -> list[UnconfirmedGroup]:
    """Approvals that wait for a person to confirm or undo them, oldest first.

    An entry counts when it was recorded with ``terminal: false``, or when its id is in
    ``also`` (the integrity check's ``approval_unconfirmed`` ids: approvals added since the git
    base without ``terminal: true``), and no later entry confirmed or undid it. Consecutive
    entries from the same draft file form one group.
    """
    settled = settled_ids(entries)
    extra = set(also)
    groups: list[UnconfirmedGroup] = []
    for e in entries:
        if e.id in settled:
            continue
        flagged = e.id in extra and e.details.get("terminal") is not True
        if not (recorded_without_terminal(e) or (flagged and e.action not in (CONFIRM, UNDO))):
            continue
        draft = e.details.get("draft_file")
        if (
            draft
            and groups
            and groups[-1].entries[-1].details.get("draft_file") == draft
            and groups[-1].entries[-1].approved_by == e.approved_by
        ):
            groups[-1].entries.append(e)
        else:
            groups.append(UnconfirmedGroup([e]))
    return groups


# ---------------------------------------------------------------------------------------------
# Copies of the earlier versions (kept when an approval is recorded without a terminal)


def snapshot(root: Path) -> dict[str, bytes]:
    """The bytes of every fingerprinted file now (taken just before an approval)."""
    out: dict[str, bytes] = {}
    for p in lock.iter_fingerprinted_files(root):
        try:
            out[paths.rel(root, p)] = p.read_bytes()
        except OSError:
            continue
    return out


def keep_copies(root: Path, before: dict[str, bytes], entries: Iterable[HistoryEntry]) -> None:
    """Save the earlier bytes of each file these approvals changed (for a later undo)."""
    for e in entries:
        if not recorded_without_terminal(e) or not e.files:
            continue
        files: dict[str, str | None] = {}
        d = undo_root(root) / e.id
        for n, (path, after) in enumerate(sorted(e.files.items())):
            data = before.get(path)
            if data is None or _sha(data) == after:
                files[path] = None  # did not exist before, or nothing to go back to
                continue
            d.mkdir(parents=True, exist_ok=True)
            name = f"{n}.txt"
            (d / name).write_bytes(data)
            files[path] = name
        if any(files.values()):
            (d / MANIFEST).write_text(
                json.dumps({"entry": e.id, "files": files}, indent=2) + "\n", encoding="utf-8"
            )


def _copy(root: Path, entry_id: str, path: str) -> bytes | None:
    d = undo_root(root) / entry_id
    try:
        manifest = as_dict(json.loads((d / MANIFEST).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    name = as_dict(manifest.get("files")).get(path)
    if not isinstance(name, str) or "/" in name or name.startswith("."):
        return None
    try:
        return (d / name).read_bytes()
    except OSError:
        return None


def _from_git(root: Path, path: str, want: str) -> bytes | None:
    """An earlier version of ``path`` from git whose fingerprint is ``want`` (or None)."""
    try:
        log = subprocess.run(
            ["git", "log", "--format=%H", "-n", "50", "--", path],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if log.returncode != 0:
        return None
    for commit in log.stdout.split():
        try:
            shown = subprocess.run(
                ["git", "show", f"{commit}:./{path}"],
                cwd=str(root),
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if shown.returncode == 0 and _sha(shown.stdout) == want:
            return shown.stdout
    return None


def _drop_copies(root: Path, ids: Iterable[str]) -> None:
    for i in ids:
        shutil.rmtree(undo_root(root) / i, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# Decisions (a person in a terminal)

_NOT_CLEAN = (
    "Law files changed outside lawkeep, so lawkeep will not record anything until a person "
    "decides about that first (it is listed in `lawkeep approve`)."
)


def confirm(root: Path, group: UnconfirmedGroup, *, person: str, channel: str) -> tuple[bool, str]:
    """Record that a person confirms this approval. Nothing on disk changes."""
    if not lock.diff_against_lock(root).is_empty:
        return False, _NOT_CLEAN
    rec = history.record_change(
        root,
        action=CONFIRM,
        actor=person,
        law_id=", ".join(group.law_ids) or None,
        reason="a person confirmed an approval that was recorded without a terminal",
        permission_used="ask",
        approved_by=person,
        details={"entries": group.ids, "channel": channel, "terminal": True},
        refers_to=group.id,
    )
    _drop_copies(root, group.ids)
    return True, f"Confirmed {', '.join(group.ids)} (recorded as {rec.entry.id})."


_OPPOSITE = {"stricter": "looser", "looser": "stricter", "same": "same"}


def undo(root: Path, group: UnconfirmedGroup, *, person: str, channel: str) -> tuple[bool, str]:
    """Put the law files back to how they were before this approval, and record it."""
    root = Path(root)
    if not lock.diff_against_lock(root).is_empty:
        return False, _NOT_CLEAN
    entries = history.read(root)
    index = {e.id: n for n, e in enumerate(entries)}
    restore: dict[str, bytes | None] = {}
    back_to_drafts: dict[str, bytes] = {}
    for e in group.entries:
        if e.id not in index:
            return False, f"{e.id} is not in the law history any more."
        before = history.latest_hashes(entries[: index[e.id]])
        draft_file = e.details.get("draft_file")
        for path, after in e.files.items():
            target = root / path
            now = lock.hash_file(target) if target.is_file() else None
            if now != after:
                return False, (
                    f"{path} changed after this approval, so undoing it could also undo that "
                    "later change. Change it back with `lawkeep propose` instead."
                )
            copy = _copy(root, e.id, path)
            if path in before:
                want = before[path]
                if want is None:
                    restore[path] = None
                    continue
                data = copy if copy is not None and _sha(copy) == want else None
                data = data if data is not None else _from_git(root, path, want)
                if data is None:
                    return False, (
                        f"lawkeep has no copy of the earlier version of {path}, so it cannot "
                        "undo this approval safely. Put the file back with git, then accept "
                        "that with `lawkeep approve --accept-unrecorded`."
                    )
                restore[path] = data
            elif copy is not None:
                restore[path] = copy
            elif e.action == "add_law" and draft_file:
                restore[path] = None  # the approved draft created this file
            else:
                return False, (
                    f"The law history does not say what {path} looked like before this "
                    "approval, so lawkeep cannot undo it safely."
                )
            if restore[path] is None and draft_file and now is not None:
                back_to_drafts[str(draft_file)] = target.read_bytes()
    notes: list[str] = []
    for draft_file, data in back_to_drafts.items():
        dest = root / draft_file
        if paths.is_draft(root, dest) and not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            notes.append(f"the draft is back in {draft_file} and waits for your decision again")
    for path, data in restore.items():
        target = root / path
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    first = group.entries[0]
    rec = history.record_change(
        root,
        action=UNDO,
        actor=person,
        law_id=", ".join(group.law_ids) or None,
        direction=_OPPOSITE.get(first.direction or "", "unclear"),
        reason="a person undid an approval that was recorded without a terminal",
        permission_used="ask",
        approved_by=person,
        sentence_before=first.sentence_after,
        sentence_after=first.sentence_before,
        details={
            "entries": group.ids,
            "undone_action": first.action,
            "channel": channel,
            "terminal": True,
        },
        refers_to=group.id,
    )
    _drop_copies(root, group.ids)
    extra = f" ({'; '.join(notes)})" if notes else ""
    return True, f"Undid {', '.join(group.ids)} (recorded as {rec.entry.id}){extra}."
