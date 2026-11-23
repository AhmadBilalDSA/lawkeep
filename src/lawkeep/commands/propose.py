"""`lawkeep propose`: propose a change to a law (weakening waits for a person's yes).

Owner: workstream D (law lifecycle).

    lawkeep propose LAW_ID --file NEW_VERSION.py --reason "why" [--actor agent:NAME]
    lawkeep propose --delete LAW_ID --reason "why"

1. Refuse while laws differ from ``laws.lock`` (an unrecorded change must be decided first).
2. Decide the direction by evidence (``lawkeep.direction``).
3. Apply the permission from ``lawkeep.toml`` (``lawkeep.permissions``):
   - auto: write the law, record it in the history (laws.lock is refreshed), result 0;
   - ask: park it under ``laws/drafts/proposals/<history id>/`` (``proposed.py.txt`` +
     ``meta.json``; ``.txt`` so neither the gate nor draft discovery picks it up), append a
     ``waiting`` history entry whose ``details.proposal_path`` points there, result 2.
   A new version that does not even import is rejected (result 1) and recorded as rejected.
   A NEW law must also pass the quick strength check (``strength.run_quick_check``: the law
   must catch dumb versions of the functions it calls) before it is applied automatically;
   a new law that does not is parked for a person's yes like a weakening, with the result.
4. A proposal identical to the law as it is changes nothing and records nothing.

``lawkeep approve <history id> --yes|--no`` decides a parked proposal (see ``approve.py``,
which uses :func:`apply_proposal` and :func:`reject_proposal` from this module).
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
from pathlib import Path
from typing import Any

from lawkeep import config, discovery, exitcodes, explain, history, lock, output, paths, ui
from lawkeep.direction import DirectionResult, decide_direction, is_skip_edit
from lawkeep.errors import SetupError
from lawkeep.models import HistoryEntry, ProposalResult
from lawkeep.permissions import decide

HELP = "ask for a change to a law; changes that weaken a law wait for a person's yes"

PROPOSALS_DIRNAME = "proposals"
_NEW_LAW = "New law — it adds a check and does not weaken any law you have."
PROPOSAL_FILE = "proposed.py.txt"
META_FILE = "meta.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "law_id", nargs="?", help="the law to change (e.g. refunds.refund_is_capped)"
    )
    parser.add_argument("--file", metavar="PATH", help="the complete new version of the law's file")
    parser.add_argument("--reason", default="", help="why the law should change (shown to people)")
    parser.add_argument(
        "--delete", action="store_true", help="propose deleting the law (always needs a yes)"
    )
    parser.add_argument(
        "--actor",
        default=None,
        help='who proposes it, e.g. "agent:claude-code" (default: agent from lawkeep.toml)',
    )


# ---------------------------------------------------------------------------------------------
# Helpers shared with approve.py


def proposals_dir(root: Path) -> Path:
    return paths.drafts_dir(root) / PROPOSALS_DIRNAME


def default_actor(cfg: config.Config) -> str:
    return f"agent:{cfg.agent.name}" if cfg.agent.name else "agent:other"


def remove_law_from_source(source: str, qualname: str) -> str:
    """Source without the law's definition (decorators included)."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == qualname.split(".")[0]:
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = node.end_lineno or node.lineno
                del lines[start - 1 : end]
                return "".join(lines)
    return source


def _target_for_new_law(root: Path, law_id: str, new_file: Path | None) -> Path:
    parts = law_id.split(".")
    if len(parts) >= 2:
        return paths.laws_dir(root).joinpath(*parts[:-1]).with_suffix(".py")
    if new_file is not None:
        return paths.laws_dir(root) / new_file.name
    raise SetupError(f"lawkeep cannot tell which file under laws/ the new law {law_id} belongs in.")


def _sentence_in(text: str, target: Path, root: Path, law_id: str) -> str | None:
    found = discovery.laws_in_source(text, file=target, root=root).by_id().get(law_id)
    return found.sentence if found else None


def _evidence_text(dres: DirectionResult) -> str:
    return str(dres.evidence.get("summary") or dres.explanation)


def _unrecorded_result(law_id: str, diff: lock.LockDiff) -> ProposalResult:
    changed = diff.paths()
    return ProposalResult(
        exit_code=exitcodes.WAITING,
        summary=(
            "WAITING FOR YOUR DECISION: laws changed outside lawkeep "
            f"({', '.join(changed)}), so lawkeep did not look at this proposal and recorded "
            "nothing. Your proposed file was left where it is; propose again once a person "
            "has decided about that change."
        ),
        law_id=law_id,
        direction="unclear",
        decision="refused",
        evidence={"unrecorded_paths": changed},
    )


def _quick_check_new_law(
    root: Path, cfg: config.Config, law_id: str
) -> tuple[bool | None, str, dict[str, Any]]:
    """Run the quick strength check on a new law that is already written in laws/.

    Returns (passed, plain-English evidence, details). ``passed`` is None when the check
    could not run or is switched off in lawkeep.toml.
    """
    if not cfg.strength.quick_check:
        return None, "quick strength check switched off in lawkeep.toml", {}
    try:
        from lawkeep import strength

        results = strength.run_quick_check(root, cfg, law_ids=[law_id])
        found = strength.result_for(results, law_id)
    except Exception as exc:  # noqa: BLE001 - reported, and the law then waits for a person
        return None, f"the quick strength check could not run ({type(exc).__name__}: {exc})", {}
    if found is None:
        return None, "the quick strength check could not measure this law", {}
    text = explain.strength_status(found).text
    caught = sum(1 for o in found.quick_check if o.caught)
    passed = not found.flags and (caught > 0 or bool(found.planted_bugs_caught))
    details: dict[str, Any] = {
        "quick_check_caught": caught,
        "quick_check_tried": len(found.quick_check),
        "planted_bugs_caught": found.planted_bugs_caught,
        "planted_bugs_total": found.planted_bugs_total,
        "flags": list(found.flags),
    }
    return passed, text, details


def _write_target(root: Path, target: Path, text: str | None, qualname: str | None) -> None:
    if text is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return
    # delete the law: the whole file if it holds only this law, else just its definition
    laws_here = discovery.laws_in_file(target, root).laws
    if len(laws_here) <= 1:
        target.unlink()
    else:
        source = target.read_text(encoding="utf-8")
        target.write_text(remove_law_from_source(source, qualname or ""), encoding="utf-8")


def park_proposal(
    root: Path,
    *,
    pid: str,
    law_id: str | None,
    action: str,
    direction: str,
    target: Path,
    new_bytes: bytes | None,
    delete: bool,
    qualname: str | None,
    actor: str,
    reason: str,
    sentence_before: str | None,
    sentence_after: str | None,
    details: dict[str, Any],
    whole_file: bool = False,
) -> HistoryEntry:
    """Park a proposal under ``laws/drafts/proposals/<pid>/`` and append its ``waiting`` entry.

    ``new_bytes`` is the complete new content of ``target`` (``None`` with ``delete``).
    ``whole_file``: the proposal replaces (or, with ``delete``, removes) the whole file, not
    one law in it (used for the edits the drafting agent made, see ``draft_edits``).
    The append-only history keeps the hashes, so a proposal swapped on disk after it was
    recorded, or a target that changed meanwhile, is refused on approval (``apply_proposal``).
    """
    pdir = proposals_dir(root) / pid
    pdir.mkdir(parents=True, exist_ok=True)
    proposal_sha = None
    if new_bytes is not None:
        (pdir / PROPOSAL_FILE).write_bytes(new_bytes)
        proposal_sha = lock.hash_file(pdir / PROPOSAL_FILE)
    base_sha = lock.hash_file(target) if target.is_file() else None
    meta: dict[str, Any] = {
        "history_id": pid,
        "law_id": law_id,
        "action": action,
        "direction": direction,
        "delete": delete,
        "target_file": paths.rel(root, target),
        "qualname": qualname,
        "base_sha256": base_sha,
        "proposal_sha256": proposal_sha,
        "actor": actor,
        "reason": reason,
    }
    if whole_file:
        meta["whole_file"] = True
    (pdir / META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    details = dict(details)
    details.update(
        {
            "proposal_path": paths.rel(root, pdir) + "/",
            "target_file": paths.rel(root, target),
            "base_sha256": base_sha,
            "proposal_sha256": proposal_sha,
            "delete": delete,
        }
    )
    if whole_file:
        details["whole_file"] = True
    entry = HistoryEntry(
        id=pid,
        action=action,
        actor=actor,
        law_id=law_id,
        direction=direction,
        reason=reason,
        permission_used="ask",
        status="waiting",
        sentence_before=sentence_before,
        sentence_after=sentence_after,
        details=details,
    )
    history.append(root, entry)
    return entry


def propose(
    root: Path,
    cfg: config.Config,
    law_id: str,
    *,
    new_text: str | None,
    reason: str,
    actor: str,
    delete: bool = False,
    source_name: str | None = None,
) -> tuple[ProposalResult, str]:
    """Core of ``lawkeep propose`` (also usable from the MCP server). Returns (result, text)."""
    root = Path(root).resolve()
    if lock.read_lock(root) is None:
        raise SetupError(
            "This project has no laws.lock yet, so lawkeep cannot record law changes.",
            hint="Run `lawkeep init` first.",
        )
    diff = lock.diff_against_lock(root)
    if not diff.is_empty:
        res = _unrecorded_result(law_id, diff)
        text = ui.render(
            ui.banner(
                "WAITING FOR YOUR DECISION",
                "Some law files were changed directly, without lawkeep (an unrecorded change), "
                "so lawkeep did not look at this proposal and recorded nothing.",
                tone="wait",
            ),
            ui.indent(ui.group(*[ui.dim(p) for p in diff.paths()])),
            ui.status("info", "Your proposed file was left where it is."),
            ui.next_step(
                "a person decides about the changed files with `lawkeep approve "
                "--accept-unrecorded` (or puts them back with git); then propose again."
            ),
        )
        return res, text

    laws = discovery.discover_laws(root).by_id()
    old = laws.get(law_id)
    if delete and old is None:
        raise SetupError(f"There is no approved law {law_id!r} to delete.")
    target = (
        (root / old.file)
        if old
        else _target_for_new_law(root, law_id, Path(source_name) if source_name else None)
    )
    if new_text is not None:
        try:
            ast.parse(new_text)
        except SyntaxError as exc:
            raise SetupError(
                f"The new version has a syntax error on line {exc.lineno} ({exc.msg}).",
                hint="Fix the file and propose again.",
            ) from None
        if old is None and _sentence_in(new_text, target, root, law_id) is None:
            ids = [
                i.law_id for i in discovery.laws_in_source(new_text, file=target, root=root).laws
            ]
            raise SetupError(
                f"The new file does not define a law with id {law_id!r} "
                f"(as {paths.rel(root, target)} it defines: {', '.join(ids) or 'no laws'}).",
                hint='Give the law @law("...", id="...") or use the id lawkeep shows.',
            )
        if old is None and target.is_file():
            before = discovery.laws_in_file(target, root).by_id()
            after = discovery.laws_in_source(new_text, file=target, root=root).by_id()
            touched = [
                i
                for i, info in before.items()
                if i not in after or after[i].source_hash != info.source_hash
            ]
            if touched:
                raise SetupError(
                    f"Adding {law_id} would also change other laws in "
                    f"{paths.rel(root, target)}: {', '.join(touched)}.",
                    hint="Propose one law change at a time.",
                )

    if old is not None and new_text is not None and not delete:
        try:
            current_text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current_text = None
        if current_text == new_text:
            res = ProposalResult(
                exit_code=exitcodes.ALL_HOLD,
                summary=(
                    f"NOTHING TO CHANGE: the proposed file is identical to "
                    f"{paths.rel(root, target)} as it is now, so nothing was recorded."
                ),
                law_id=law_id,
                direction="same",
                decision="unchanged",
                reason=reason,
            )
            text = ui.render(
                ui.banner(
                    "NOTHING TO CHANGE",
                    "The proposed file is exactly the law as it is now, so nothing was recorded.",
                    tone="ok",
                ),
                ui.ident(f"law id {law_id} · file {paths.rel(root, target)}"),
                ui.next_step("nothing to do."),
            )
            return res, text

    pid = history.new_entry_id()
    staging_dir = paths.state_dir(root) / "tmp" / f"propose-{pid}"
    staged: Path | None = None
    if new_text is not None:
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = staging_dir / target.name
        staged.write_text(new_text, encoding="utf-8")
    try:
        dres = decide_direction(root, cfg, law_id, target if old else None, staged)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    sentence_before = old.sentence if old else None
    sentence_after = (
        None if delete else (_sentence_in(new_text or "", target, root, law_id) or sentence_before)
    )
    if delete:
        action = "remove_law"
    elif old is None:
        action = "add_law"
    elif is_skip_edit(
        target.read_text(encoding="utf-8"), old.qualname, new_text or "", old.qualname
    ):
        action = "skip_law"
    elif dres.direction == "same":
        action = "rename_target"
    else:
        action = "change_law"
    rows = [r.to_dict() for r in dres.examples]
    evidence_text = _evidence_text(dres)

    try:
        if dres.new_version_broken:
            rec = history.record_change(
                root,
                action=action,
                actor=actor,
                law_id=law_id,
                direction=dres.direction,
                reason=reason,
                permission_used="none",
                status="rejected",
                sentence_before=sentence_before,
                sentence_after=sentence_after,
                details={
                    "evidence": evidence_text,
                    "rejected_because": "the new version does not run",
                },
            )
            res = ProposalResult(
                exit_code=exitcodes.SOME_FAIL,
                summary=f"REJECTED: the new version of {law_id} does not run. {evidence_text}",
                law_id=law_id,
                direction=dres.direction,
                decision="rejected",
                reason=reason,
                evidence=dres.evidence,
                history_id=rec.entry.id,
            )
            text = ui.render(
                ui.banner(
                    "REJECTED",
                    "The new version of the law does not run, so lawkeep rejected it. The law "
                    "stays as it was.",
                    tone="fail",
                ),
                ui.status("fail", explain.plain_explanation(evidence_text)),
                ui.ident(f"law id {law_id} · recorded as {rec.entry.id}"),
                ui.next_step("fix the new version and propose it again."),
            )
            return res, text

        perm = decide(cfg, action, dres.direction)
        details: dict[str, Any] = {
            "evidence": evidence_text,
            "direction_evidence": dres.evidence,
            "explanation": dres.explanation,
            "examples": rows,
            "permission_key": perm.key,
        }
        may_apply = perm.may_apply
        strength_note = None
        if may_apply and action == "add_law" and new_text is not None:
            # Every new law must pass the quick strength check before it goes in unasked.
            before = target.read_text(encoding="utf-8") if target.is_file() else None
            _write_target(root, target, new_text, None)
            passed, qtext, qdetails = _quick_check_new_law(root, cfg, law_id)
            if before is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(before, encoding="utf-8")
            evidence_text = qtext or evidence_text
            details["evidence"] = evidence_text
            details["strength"] = qdetails
            if passed is not True and cfg.strength.quick_check:
                may_apply = False
                strength_note = (
                    f"The new law did not pass the quick strength check ({qtext}), so it waits "
                    f"for a person's yes instead of going in automatically."
                )
                details["explanation"] = strength_note
        if may_apply:
            _write_target(root, target, None if delete else new_text, old.qualname if old else None)
            rec = history.record_change(
                root,
                action=action,
                actor=actor,
                law_id=law_id,
                direction=dres.direction,
                reason=reason,
                permission_used="auto",
                status="applied",
                sentence_before=sentence_before,
                sentence_after=sentence_after,
                details=details,
            )
            res = ProposalResult(
                exit_code=exitcodes.ALL_HOLD,
                summary=(
                    f"APPLIED: {law_id} changed automatically ({dres.direction}). "
                    f"{perm.explanation}"
                ),
                law_id=law_id,
                direction=dres.direction,
                decision="applied",
                permission_key=perm.key,
                permission_value=perm.value,
                reason=reason,
                evidence=dres.evidence,
                examples=dres.examples,
                history_id=rec.entry.id,
            )
            text = ui.render(
                ui.banner(
                    "APPLIED",
                    f"The law change went in without asking. {perm.explanation}",
                    tone="ok",
                ),
                ui.panel(
                    ui.status(
                        "ok" if dres.direction in ("stricter", "same") else "warn",
                        (_NEW_LAW if old is None else explain.direction_text(dres.direction)),
                    ),
                    ui.blank(),
                    ui.label_value(
                        [
                            (
                                "Law before:",
                                f"“{sentence_before}”" if sentence_before else "(no law yet)",
                            ),
                            (
                                "Law now:",
                                "(deleted)"
                                if delete
                                else (f"“{sentence_after}”" if sentence_after else "-"),
                            ),
                            ("Why:", reason or "(no reason given)"),
                            ("Evidence:", explain.plain_explanation(evidence_text)),
                        ]
                    ),
                    ui.ident(f"law id {law_id} · recorded as {rec.entry.id}"),
                    title="Law change",
                    tone="ok",
                ),
                ui.next_step(
                    "run `lawkeep check`. The person can see every change with `lawkeep report`."
                ),
            )
            return res, text

        # ask: park the proposal and wait for a person
        park_proposal(
            root,
            pid=pid,
            law_id=law_id,
            action=action,
            direction=dres.direction,
            target=target,
            new_bytes=None if new_text is None else new_text.encode("utf-8"),
            delete=delete,
            qualname=old.qualname if old else None,
            actor=actor,
            reason=reason,
            sentence_before=sentence_before,
            sentence_after=sentence_after,
            details=details,
        )
        res = ProposalResult(
            exit_code=exitcodes.WAITING,
            summary=(
                f"WAITING FOR YOUR YES: {strength_note}"
                if strength_note
                else (
                    f"WAITING FOR YOUR YES: {law_id} would become {dres.direction.upper()}. "
                    f"{perm.explanation}"
                )
            ),
            law_id=law_id,
            direction=dres.direction,
            decision="waiting",
            permission_key=perm.key,
            permission_value=perm.value,
            reason=reason,
            evidence=dres.evidence,
            examples=dres.examples,
            history_id=pid,
        )
        if delete:
            after = "(the law would be deleted)"
        elif action == "skip_law":
            after = "(the law would be skipped: lawkeep would stop checking it)"
        elif sentence_after == sentence_before:
            after = "the same sentence, but what the law checks changes"
        else:
            after = f"“{sentence_after}”"
        head = (
            "This new law is not strong enough to go in without asking."
            if strength_note
            else (_NEW_LAW if old is None else explain.direction_text(dres.direction))
        )
        body = [
            ui.status("ok" if dres.direction in ("stricter", "same") else "warn", head),
            ui.blank(),
            ui.label_value(
                [
                    ("Law now:", f"“{sentence_before}”" if sentence_before else "(no law yet)"),
                    ("Would become:", after),
                    ("Why:", reason or "(no reason given)"),
                    (
                        "What changes:",
                        explain.plain_explanation(strength_note or dres.explanation),
                    ),
                ]
            ),
        ]
        if dres.examples:
            body += [
                ui.blank(),
                ui.bold("Examples: does each version of the law accept what the code did?"),
                explain.examples_table(dres.examples, proposal=True),
            ]
        body.append(ui.ident(f"law id {law_id} · decision id {pid}"))
        from lawkeep import presence

        seen = presence.detect()
        res.person_present = seen.present
        if seen.present:
            res.next_step = (
                f"Show this to the person. They decide with `lawkeep approve {pid} --yes` or "
                f"`lawkeep approve {pid} --no` (or just `lawkeep approve` in a terminal)."
            )
        else:
            res.next_step = parked_next_step(pid)
        text = ui.render(
            ui.banner(
                "WAITING FOR YOUR YES",
                "This law change was not applied yet. A person must say yes or no first.",
                tone="wait",
            ),
            ui.panel(*body, title="Proposed law change", tone="warn"),
            ui.next_step(res.next_step),
        )
        return res, text
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def parked_next_step(pid: str) -> str:
    """What an agent with nobody to ask does after a proposal waits (park and keep going)."""
    return (
        f"Parked as {pid}. Continue with work that does not depend on this law; list {pid} "
        "in your final summary. The person decides later with `lawkeep approve`."
    )


def read_meta(root: Path, entry: HistoryEntry) -> dict[str, Any] | None:
    p = entry.details.get("proposal_path")
    if not p:
        return None
    meta_path = Path(root) / p / META_FILE
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def apply_proposal(
    root: Path, entry: HistoryEntry, *, approved_by: str, channel: str, terminal: bool = False
) -> tuple[bool, str]:
    """Apply a parked proposal after a person's yes. Returns (ok, plain-English message)."""
    root = Path(root).resolve()
    meta = read_meta(root, entry)
    if meta is None:
        return False, (
            f"The parked proposal for {entry.law_id or entry.details.get('target_file')} ({entry.id}) is missing from "
            f"{entry.details.get('proposal_path')}; reject it and propose again."
        )
    pdir = root / entry.details["proposal_path"]
    # Trust the history entry (append-only, checked) over meta.json (a plain file on disk).
    recorded = entry.details
    target_rel = recorded.get("target_file") or meta["target_file"]
    base_sha = recorded["base_sha256"] if "base_sha256" in recorded else meta.get("base_sha256")
    delete = bool(recorded["delete"] if "delete" in recorded else meta.get("delete"))
    target = root / target_rel
    current = lock.hash_file(target) if target.is_file() else None
    if current != base_sha:
        return False, (
            f"{target_rel} changed after this proposal was made, so applying it could "
            f"undo that change. Reject it ({entry.id}) and propose again."
        )
    if not delete and recorded.get("proposal_sha256"):
        on_disk = pdir / PROPOSAL_FILE
        if not on_disk.is_file() or lock.hash_file(on_disk) != recorded["proposal_sha256"]:
            return False, (
                f"The parked proposal for {entry.law_id or entry.details.get('target_file')} ({entry.id}) was changed after it was "
                f"recorded, so it is not what you were shown. Reject it ({entry.id}) and "
                f"propose again."
            )
    whole_file = bool(
        recorded["whole_file"] if "whole_file" in recorded else meta.get("whole_file")
    )
    if whole_file:
        # the drafting agent's edit of a whole file (draft_edits): restore it byte for byte
        if delete:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((pdir / PROPOSAL_FILE).read_bytes())
    elif delete:
        _write_target(root, target, None, meta.get("qualname"))
    else:
        text = (pdir / PROPOSAL_FILE).read_text(encoding="utf-8")
        _write_target(root, target, text, None)
    details = {
        k: entry.details.get(k)
        for k in (
            "evidence",
            "examples",
            "permission_key",
            "explanation",
            "from_draft",
            "target_file",
            "changes",
            "laws",
        )
        if k in entry.details
    }
    details["channel"] = channel
    details["terminal"] = terminal
    rec = history.record_change(
        root,
        action=entry.action,
        actor=entry.actor,
        law_id=entry.law_id,
        direction=entry.direction,
        reason=entry.reason,
        permission_used="ask",
        approved_by=approved_by,
        status="applied",
        sentence_before=entry.sentence_before,
        sentence_after=entry.sentence_after,
        details=details,
        refers_to=entry.id,
    )
    shutil.rmtree(pdir, ignore_errors=True)
    what = entry.law_id or target_rel
    return True, f"Applied the proposal for {what} (recorded as {rec.entry.id})."


def reject_proposal(
    root: Path, entry: HistoryEntry, *, decided_by: str, channel: str
) -> tuple[bool, str]:
    root = Path(root).resolve()
    history.record_change(
        root,
        action=entry.action,
        actor=decided_by,
        law_id=entry.law_id,
        direction=entry.direction,
        reason=entry.reason,
        permission_used="ask",
        status="rejected",
        sentence_before=entry.sentence_before,
        sentence_after=entry.sentence_after,
        details={"channel": channel, "decided_by": decided_by},
        refers_to=entry.id,
    )
    p = entry.details.get("proposal_path")
    if p:
        shutil.rmtree(root / p, ignore_errors=True)
    if entry.law_id:
        return True, f"Rejected the proposed change to {entry.law_id}; the law stays as it was."
    target = entry.details.get("target_file") or "the file"
    return True, f"Rejected the proposed change to {target}; it stays as it was."


# ---------------------------------------------------------------------------------------------


def _tidy_source(root: Path, src: Path, text: str) -> str:
    """Remove the proposed file when it sat in laws/drafts/ (outside proposals/).

    Its content is now applied or parked under laws/drafts/proposals/<id>/; left in place it
    would show up again in `lawkeep approve` as a separate new draft with the same law id.
    """
    drafts = paths.drafts_dir(root).resolve()
    try:
        parts = src.resolve().relative_to(drafts).parts
    except (ValueError, OSError):
        return text
    if not parts or parts[0] == "proposals" or not src.is_file():
        return text
    try:
        src.unlink()
    except OSError:
        return text
    return (
        text.rstrip("\n")
        + "\n"
        + ui.render(
            ui.dim(
                f"(lawkeep removed {paths.rel(root, src)}; its content is kept with the change.)"
            )
        )
    )


def run(args: argparse.Namespace) -> int:
    root = paths.require_root(args.root)
    cfg = config.load_config(root)
    if not args.law_id:
        raise SetupError(
            "Say which law to change, e.g. `lawkeep propose refunds.refund_is_capped --file "
            'new_version.py --reason "..."`.'
        )
    new_text = None
    source_name = None
    src: Path | None = None
    if args.delete:
        if args.file:
            raise SetupError("Use either --delete or --file, not both.")
    else:
        if not args.file:
            raise SetupError(
                "Give the complete new version of the law's file with --file PATH "
                "(or use --delete to propose deleting the law)."
            )
        src = Path(args.file)
        if not src.is_absolute():
            # relative to the current folder, else to the project root (agents and the MCP
            # server usually give paths like laws/drafts/x.py)
            src = Path.cwd() / src if (Path.cwd() / src).is_file() else root / src
        try:
            new_text = src.read_text(encoding="utf-8")
        except OSError as exc:
            raise SetupError(f"Cannot read {args.file}: {exc}") from None
        source_name = src.name
    res, text = propose(
        root,
        cfg,
        args.law_id,
        new_text=new_text,
        reason=args.reason or "",
        actor=args.actor or default_actor(cfg),
        delete=args.delete,
        source_name=source_name,
    )
    if src is not None and res.history_id and res.decision in ("applied", "waiting"):
        text = _tidy_source(root, src, text)
    if cfg.warnings:
        text = ui.render(*[ui.status("info", f"lawkeep.toml: {w}") for w in cfg.warnings]) + text
    output.emit(res, args.json, text=text)
    return res.exit_code
