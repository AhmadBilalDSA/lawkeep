"""Edits the drafting agent made outside ``laws/drafts/`` become waiting proposals.

Owner: workstream D (law lifecycle). Called by ``commands/draft.py`` after the drafting run.

The drafting agent started by ``lawkeep draft`` may only write drafts in ``laws/drafts/`` and
ADD input generators to ``laws/strategies.py``. When it edits anything else that ``laws.lock``
fingerprints (an approved law file, an existing generator, ``lawkeep.toml``, a helper file in
``laws/``, the lawkeep workflow), lawkeep does not throw the work away and does not apply it:

1. :func:`capture_and_restore` keeps the old and the new content of each changed file and puts
   the approved version back on disk, so ``lawkeep check`` keeps testing the approved laws and
   there is no unrecorded law change.
2. :func:`make_proposals` turns each changed file into ONE waiting proposal with the proposal
   machinery of ``lawkeep propose`` (``laws/drafts/proposals/<id>/``, hashes in the
   append-only history, ``whole_file`` = the proposal is the complete new file). The direction
   is judged as in ``propose`` (``direction.decide_direction`` for each changed law, by
   evidence, with its deterministic seed); the permission key is the one ``propose`` would
   use, but the change is NEVER applied automatically, even when it is stricter: the person
   asked to review every such edit (PLAN.md Decisions, 2026-11-22 "Drafting", approved
   2026-11-23 with this change). ``lawkeep approve`` applies or rejects it.

History fields of such a proposal: ``actor`` = the drafting agent, ``reason`` =
:data:`REASON` (plus what the agent said on a ``Changed <path>: <why>`` line), and in
``details``: ``from_draft: true``, ``kind`` (``law`` | ``generators`` | ``settings`` |
``file``), ``changes`` (plain-English list), ``laws`` (law ids in the file that changed),
``diff`` (a short unified diff), ``new_version_runs`` (False when the new law version does not
import), plus the usual ``evidence`` / ``examples`` / ``explanation`` / ``permission_key``.
"""

from __future__ import annotations

import ast
import difflib
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from lawkeep import discovery, history, paths
from lawkeep.config import Config
from lawkeep.models import ExampleRow, LawInfo
from lawkeep.permissions import decide

if TYPE_CHECKING:
    from lawkeep.direction import DirectionResult

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

REASON = "edited by the drafting agent during `lawkeep draft`"
#: Why these proposals wait even when they are stricter (shown to the person).
WAIT_WHY = (
    "Changes the drafting AI makes to approved laws, input generators or settings never go in "
    "on their own, even stricter ones: you review each one."
)
MAX_DIFF_LINES = 80
_REASON_LINE = re.compile(r"^\s*(?:[-*]\s*)?Changed\s+`?([^`:\s]+)`?\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class DraftEdit:
    """One fingerprinted file the drafting agent changed (``None`` = missing)."""

    path: str  # relative to the project root, forward slashes
    old: bytes | None
    new: bytes | None


@dataclass
class WaitingEdit:
    """A drafting edit turned into a waiting proposal (what ``draft`` shows and puts in JSON)."""

    history_id: str
    file: str
    kind: str  # "law" | "generators" | "settings" | "file"
    action: str
    direction: str
    law_id: str | None = None
    laws: list[str] = field(default_factory=list[str])
    sentence_before: str | None = None
    sentence_after: str | None = None
    reason: str = ""
    explanation: str = ""
    evidence: str = ""
    changes: list[str] = field(default_factory=list[str])
    examples: list[ExampleRow] = field(default_factory=list["ExampleRow"])
    new_version_runs: bool = True
    deleted: bool = False
    created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_id": self.history_id,
            "file": self.file,
            "kind": self.kind,
            "action": self.action,
            "direction": self.direction,
            "law_id": self.law_id,
            "laws": list(self.laws),
            "sentence_before": self.sentence_before,
            "sentence_after": self.sentence_after,
            "reason": self.reason,
            "explanation": self.explanation,
            "evidence": self.evidence,
            "changes": list(self.changes),
            "examples": [r.to_dict() for r in self.examples],
            "new_version_runs": self.new_version_runs,
            "deleted": self.deleted,
            "created": self.created,
        }


# ---------------------------------------------------------------------------------------------
# Capture and restore


def capture_and_restore(
    root: Path, before: dict[str, bytes], after: dict[str, bytes], *, skip: set[str]
) -> list[DraftEdit]:
    """Every file whose bytes differ between the snapshots, put back to its ``before`` state."""
    edits: list[DraftEdit] = []
    for relp in sorted(set(before) | set(after)):
        if relp in skip or before.get(relp) == after.get(relp):
            continue
        restore(root, relp, before.get(relp))
        edits.append(DraftEdit(relp, before.get(relp), after.get(relp)))
    return edits


def restore(root: Path, relp: str, content: bytes | None) -> None:
    p = root / relp
    if content is None:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


def agent_reasons(agent_output: str) -> dict[str, str]:
    """``Changed <path>: <why>`` lines from the agent's final message (path -> why)."""
    out: dict[str, str] = {}
    for m in _REASON_LINE.finditer(agent_output or ""):
        out.setdefault(m.group(1).strip().removeprefix("./"), m.group(2)[:500])
    return out


# ---------------------------------------------------------------------------------------------
# laws/strategies.py: keep pure additions apart from changes to existing generators


def _top_nodes(tree: ast.Module) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.setdefault(node.name, node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.setdefault(node.target.id, node)
    return out


def _segment(source: str, node: ast.stmt) -> str:
    lines = source.splitlines(keepends=True)
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(d.lineno for d in decorators)])
    end = node.end_lineno or node.lineno
    return "".join(lines[start - 1 : end])


def additions_only(old: str, new: str, added: list[str]) -> str | None:
    """``old`` plus only the generators (and imports) ``new`` ADDS, or ``None`` if that cannot
    be built cleanly. Used when the agent both added generators and changed existing ones: the
    additions follow the usual rule, the changes become a proposal."""
    if not added:
        return None
    try:
        old_tree = ast.parse(old) if old else ast.Module(body=[], type_ignores=[])
        new_tree = ast.parse(new)
    except SyntaxError:
        return None
    old_imports = {
        ast.dump(n) for n in old_tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    new_imports = [
        _segment(new, n)
        for n in new_tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom)) and ast.dump(n) not in old_imports
    ]
    nodes = _top_nodes(new_tree)
    seen: set[int] = set()
    parts: list[str] = []
    for node in new_tree.body:  # keep the new file's order
        if id(node) in seen or not any(nodes.get(n) is node for n in added):
            continue
        seen.add(id(node))
        parts.append(_segment(new, node))
    if not parts:
        return None
    text = old.rstrip("\n") + "\n" if old.strip() else ""
    if new_imports:
        text += "\n" + "".join(new_imports)
    text += "\n\n" + "\n\n".join(p.rstrip("\n") + "\n" for p in parts)
    try:
        ast.parse(text)
    except SyntaxError:
        return None
    return text


# ---------------------------------------------------------------------------------------------
# What changed, in plain words


def _text(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8", "replace")


def short_diff(relp: str, old: str | None, new: str | None) -> str:
    lines = list(
        difflib.unified_diff(
            (old or "").splitlines(),
            (new or "").splitlines(),
            fromfile=f"{relp} (approved)",
            tofile=f"{relp} (drafting agent)",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + [f"... ({len(lines) - MAX_DIFF_LINES} more lines)"]
    return "\n".join(lines)


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(cast("dict[str, Any]", v), key))
        else:
            out[key] = v
    return out


def settings_changes(old: str | None, new: str | None) -> list[str]:
    """``["check.profiles.pr.examples: 200 → 20", ...]`` between two lawkeep.toml texts."""
    if new is None:
        return ["lawkeep.toml would be deleted"]
    try:
        a = _flatten(tomllib.loads(old or ""))
    except tomllib.TOMLDecodeError:
        a = {}
    try:
        b = _flatten(tomllib.loads(new))
    except tomllib.TOMLDecodeError as exc:
        return [f"lawkeep.toml would no longer be valid settings ({exc})"]
    out: list[str] = []
    for k in sorted(set(a) | set(b)):
        if k not in b:
            out.append(f"{k}: {a[k]!r} removed")
        elif k not in a:
            out.append(f"{k}: added as {b[k]!r}")
        elif a[k] != b[k]:
            out.append(f"{k}: {a[k]!r} → {b[k]!r}")
    return out or ["only comments or layout changed"]


def _laws_using(root: Path, names: list[str]) -> list[str]:
    """Approved laws whose file mentions one of these generator names (a plain text search)."""
    if not names:
        return []
    out: list[str] = []
    try:
        laws = discovery.discover_laws(root).laws
    except Exception:  # noqa: BLE001 - only used to tell the person what may be affected
        return []
    texts: dict[str, str] = {}
    for info in laws:
        if info.file not in texts:
            try:
                texts[info.file] = (root / info.file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                texts[info.file] = ""
        if any(re.search(rf"\b{re.escape(n)}\s*\(", texts[info.file]) for n in names):
            out.append(info.law_id)
    return out


# ---------------------------------------------------------------------------------------------
# Direction of a law-file edit (as `lawkeep propose` judges it)


@dataclass
class _Judgement:
    action: str
    direction: str
    law_id: str | None
    laws: list[str]
    sentence_before: str | None
    sentence_after: str | None
    qualname: str | None
    explanation: str
    evidence: str
    evidence_data: dict[str, Any]
    examples: list[ExampleRow]
    new_version_runs: bool = True
    changes: list[str] = field(default_factory=list[str])


def _combine(directions: list[str]) -> str:
    """One label for several laws: any looser -> looser, any unclear -> unclear, ..."""
    for d in ("looser", "unclear", "stricter", "same"):
        if d in directions:
            return d
    return "unclear"


def _judge_law_file(root: Path, cfg: Config, edit: DraftEdit) -> _Judgement | None:
    """Direction of an edit to a file under laws/ that holds laws (None: it holds no law)."""
    from lawkeep.direction import decide_direction, is_skip_edit

    target = root / edit.path
    old_text, new_text = _text(edit.old), _text(edit.new)
    old_laws: dict[str, LawInfo] = (
        discovery.laws_in_source(old_text, file=target, root=root).by_id() if old_text else {}
    )
    new_laws: dict[str, LawInfo] = (
        discovery.laws_in_source(new_text, file=target, root=root).by_id() if new_text else {}
    )
    if not old_laws and not new_laws:
        return None
    first_old = next(iter(old_laws.values()), None)
    if new_text is None:
        ids = list(old_laws)
        return _Judgement(
            action="remove_law",
            direction="looser",
            law_id=ids[0] if ids else None,
            laws=ids,
            sentence_before=first_old.sentence if first_old else None,
            sentence_after=None,
            qualname=first_old.qualname if first_old else None,
            explanation=(
                f"The drafting agent deleted {edit.path}, which deletes "
                f"{'its law' if len(ids) == 1 else f'its {len(ids)} laws'}: "
                "nothing would check those rules any more."
            ),
            evidence="the file and its laws are deleted",
            evidence_data={"static": ["the file and its laws are deleted"]},
            examples=[],
            changes=[f"deletes law {i}" for i in ids],
        )
    added = [i for i in new_laws if i not in old_laws]
    removed = [i for i in old_laws if i not in new_laws]
    changed = [
        i for i in old_laws if i in new_laws and new_laws[i].source_hash != old_laws[i].source_hash
    ]
    renamed_to: str | None = None
    if len(old_laws) == 1 and len(new_laws) == 1 and removed and added:
        # one law renamed: decide_direction compares it with its new name
        renamed_to = added[0]
        changed, removed, added = [removed[0]], [], []
    if not (added or removed or changed):
        # only the rest of the file changed (imports, helpers): every law in it may be affected
        changed = list(old_laws)
    affected = changed + removed + added
    changes = (
        [
            f"changes law {i}" + (f" (renamed to {renamed_to})" if renamed_to else "")
            for i in changed
        ]
        + [f"deletes law {i}" for i in removed]
        + [f"adds law {i}" for i in added]
    )

    results: list[tuple[str, DirectionResult]] = []
    staging = paths.state_dir(root) / "tmp" / f"draft-edit-{history.new_entry_id()}"
    try:
        staged: Path | None = None
        if old_text is not None:
            staging.mkdir(parents=True, exist_ok=True)
            staged = staging / target.name
            staged.write_bytes(edit.new or b"")
        for law_id in affected:
            if law_id in added or old_text is None:
                results.append((law_id, decide_direction(root, cfg, law_id, None, target)))
            elif law_id in removed:
                results.append((law_id, decide_direction(root, cfg, law_id, target, None)))
            else:
                assert staged is not None
                results.append((law_id, decide_direction(root, cfg, law_id, target, staged)))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    direction = _combine([r.direction for _, r in results])
    main_id = affected[0]
    main_old = old_laws.get(main_id)
    main_new = new_laws.get(renamed_to or main_id)
    if len(affected) == 1:
        if old_text is None or main_id in added:
            action = "add_law"
        elif main_id in removed:
            action = "remove_law"
        elif (
            main_old is not None
            and main_new is not None
            and is_skip_edit(old_text, main_old.qualname, new_text, main_new.qualname)
        ):
            action = "skip_law"
        elif direction == "same":
            action = "rename_target"
        else:
            action = "change_law"
    else:
        action = "add_law" if old_text is None else "change_law"
    broken = any(r.new_version_broken for _, r in results)
    if len(results) == 1:
        res = results[0][1]
        explanation = res.explanation
        evidence = str(res.evidence.get("summary") or res.explanation)
        evidence_data: dict[str, Any] = dict(res.evidence)
    else:
        explanation = " ".join(f"{i}: {r.explanation}" for i, r in results)
        evidence = "; ".join(f"{i}: {r.evidence.get('summary') or r.direction}" for i, r in results)
        evidence_data = {i: r.evidence for i, r in results}
    examples: list[ExampleRow] = next(
        (list(r.examples) for _, r in results if r.examples), list[ExampleRow]()
    )
    if broken:
        explanation = (
            "The drafting agent's version of this law does not run, so approving it would make "
            "`lawkeep check` fail. " + explanation
        )
    return _Judgement(
        action=action,
        direction=direction,
        law_id=main_id,
        laws=affected,
        sentence_before=main_old.sentence if main_old else None,
        sentence_after=main_new.sentence if main_new else None,
        qualname=main_old.qualname if main_old else None,
        explanation=explanation,
        evidence=evidence,
        evidence_data=evidence_data,
        examples=examples,
        new_version_runs=not broken,
        changes=changes,
    )


def _judge_other(root: Path, edit: DraftEdit) -> tuple[str, str, list[str], str]:
    """(kind, action, changes, explanation) for an edit that is not to a law file."""
    old_text, new_text = _text(edit.old), _text(edit.new)
    if edit.path == paths.CONFIG_FILENAME:
        return (
            "settings",
            "change_settings",
            settings_changes(old_text, new_text),
            "Settings apply to every law (for example how many examples each law tries, and "
            "which changes need your yes), so lawkeep cannot measure what this does to each "
            "law. Read the change.",
        )
    if edit.path == paths.rel(root, paths.strategies_path(root)):
        from lawkeep.commands.draft import strategies_change

        _only, added, changed = strategies_change(old_text or "", new_text or "")
        changes: list[str] = []
        if changed:
            changes.append(f"changes or removes existing generators: {', '.join(changed)}")
        if added:
            changes.append(f"adds generators: {', '.join(added)}")
        using = _laws_using(root, [c for c in changed if not c.startswith("(")])
        if using:
            changes.append(f"laws that use them: {', '.join(using)}")
        return (
            "generators",
            "change_strategy",
            changes or ["changes laws/strategies.py"],
            "It changes input generators approved laws already use. That changes which inputs "
            "those laws try, and lawkeep cannot tell from samples whether the new inputs still "
            "cover everything the old ones did.",
        )
    if new_text is None:
        what = f"deletes {edit.path}"
    elif old_text is None:
        what = f"creates {edit.path}"
    else:
        what = f"changes {edit.path}"
    return (
        "file",
        "change_law_file",
        [what],
        "This file is used when your laws run (or by lawkeep itself), so changing it can "
        "change what the laws check. lawkeep cannot measure the effect; read the change.",
    )


# ---------------------------------------------------------------------------------------------


def make_proposals(
    root: Path, cfg: Config, edits: list[DraftEdit], *, actor: str, agent_output: str = ""
) -> list[WaitingEdit]:
    """Turn each captured edit (already restored on disk) into a waiting proposal."""
    from lawkeep.commands.propose import park_proposal

    if not edits:
        return []
    reasons = agent_reasons(agent_output)
    out: list[WaitingEdit] = []
    for edit in edits:
        target = root / edit.path
        why = reasons.get(edit.path)
        reason = f"{REASON}; the agent said: {why}" if why else REASON
        old_text, new_text = _text(edit.old), _text(edit.new)
        judged = (
            _judge_law_file(root, cfg, edit)
            if edit.path.endswith(".py")
            and edit.path != paths.rel(root, paths.strategies_path(root))
            else None
        )
        if judged is not None:
            kind = "law"
            action, direction = judged.action, judged.direction
            law_id, laws = judged.law_id, judged.laws
            sentence_before, sentence_after = judged.sentence_before, judged.sentence_after
            explanation, evidence = judged.explanation, judged.evidence
            changes, examples = judged.changes, judged.examples
            qualname, runs = judged.qualname, judged.new_version_runs
            evidence_data = judged.evidence_data
        else:
            kind, action, changes, explanation = _judge_other(root, edit)
            direction = "looser" if new_text is None else "unclear"
            law_id, laws, qualname, runs, examples = None, [], None, True, []
            sentence_before = None
            label = {
                "settings": "settings in lawkeep.toml",
                "generators": "input generators in laws/strategies.py",
            }.get(kind, edit.path)
            sentence_after = f"{label}: " + "; ".join(changes[:3])
            evidence = "; ".join(changes)
            evidence_data = {"summary": evidence}
        perm = decide(cfg, action, direction)
        details: dict[str, Any] = {
            "from_draft": True,
            "kind": kind,
            "evidence": evidence,
            "direction_evidence": evidence_data,
            "explanation": explanation,
            "examples": [r.to_dict() for r in examples],
            "permission_key": perm.key,
            "waits_because": WAIT_WHY,
            "changes": changes,
            "laws": laws,
            "diff": short_diff(edit.path, old_text, new_text),
            "new_version_runs": runs,
        }
        if why:
            details["agent_reason"] = why
        pid = history.new_entry_id()
        park_proposal(
            root,
            pid=pid,
            law_id=law_id,
            action=action,
            direction=direction,
            target=target,
            new_bytes=edit.new,
            delete=edit.new is None,
            qualname=qualname,
            actor=actor,
            reason=reason,
            sentence_before=sentence_before,
            sentence_after=sentence_after,
            details=details,
            whole_file=True,
        )
        out.append(
            WaitingEdit(
                history_id=pid,
                file=edit.path,
                kind=kind,
                action=action,
                direction=direction,
                law_id=law_id,
                laws=laws,
                sentence_before=sentence_before,
                sentence_after=sentence_after,
                reason=reason,
                explanation=explanation,
                evidence=evidence,
                changes=changes,
                examples=examples,
                new_version_runs=runs,
                deleted=edit.new is None,
                created=edit.old is None,
            )
        )
    return out
