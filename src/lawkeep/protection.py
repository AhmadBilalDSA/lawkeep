"""Protection report: which changed code no law protects ("only review what's unprotected").

Owner: workstream C (strength & protection).

git diff against the base ref -> changed lines mapped to functions (ast) -> planted bugs only
in those functions (strength.catch_matrix) -> each function protected / partly / not_protected.

Base ref: ``--base REF`` (diffed from the merge-base of REF and HEAD, like a pull request), or
by default the merge-base with ``origin/main``, ``main``, ``origin/master`` or ``master``.
The diff includes uncommitted and new (untracked) files, so it also works at the end of an
agent session. Without git, every public function is checked instead (and the report says so).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lawkeep import exitcodes, paths
from lawkeep.config import Config
from lawkeep.errors import SetupError
from lawkeep.models import ProtectionReport, ProtectionResult
from lawkeep.strength import sourcemap as sm

#: Planted bugs per changed function (evenly spread); keeps the report fast on big functions.
MAX_BUGS_PER_FUNCTION = 20
#: Planted bugs per function when there is no git and every public function is checked.
MAX_BUGS_NO_GIT = 8
DEFAULT_BASES = ("origin/main", "main", "origin/master", "master")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangedFunction:
    function: str  # "billing.refunds:refund"
    file: str  # relative path
    changed_lines: list[int] = field(default_factory=list[int])

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ChangeSet:
    """What changed, worked out from git (no code runs)."""

    base_ref: str | None = None  # what the user asked for / what was picked
    base_commit: str | None = None  # the commit diffed against (None = no commits yet)
    label: str = ""  # plain English, e.g. "main (merge-base 1a2b3c4d)"
    git: bool = True  # False when git is missing or this is not a repository
    functions: list[ChangedFunction] = field(default_factory=list["ChangedFunction"])
    deleted: list[str] = field(default_factory=list[str])  # functions removed since the base
    module_level: dict[str, list[int]] = field(
        default_factory=dict[str, list[int]]
    )  # changed lines outside functions
    notes: list[str] = field(default_factory=list[str])


# ---------------------------------------------------------------------------------------------
# git helpers


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout


def in_git_repo(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def _commit_of(root: Path, ref: str) -> str | None:
    code, out = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if code == 0 and out.strip() else None


def _merge_base(root: Path, a: str, b: str) -> str | None:
    code, out = _git(root, "merge-base", a, b)
    return out.strip() if code == 0 and out.strip() else None


def resolve_base(root: Path, base_ref: str | None) -> tuple[str | None, str, list[str]]:
    """(commit to diff against or None when there are no commits, plain label, notes)."""
    notes: list[str] = []
    head = _commit_of(root, "HEAD")
    if base_ref:
        commit = _commit_of(root, base_ref)
        if commit is None:
            raise SetupError(
                f"The base {base_ref!r} is not a branch, tag or commit in this repository.",
                hint="Pass an existing one, e.g. `lawkeep report --base main` "
                "(in CI, fetch it first: `git fetch origin main`).",
            )
        if head is None:
            return commit, base_ref, notes
        mb = _merge_base(root, "HEAD", commit)
        if mb and mb != commit:
            return mb, f"{base_ref} (merge-base {mb[:8]})", notes
        return commit, base_ref, notes
    if head is None:
        notes.append("This repository has no commits yet, so every file counts as new.")
        return None, "no commits yet (everything is new)", notes
    for cand in DEFAULT_BASES:
        commit = _commit_of(root, cand)
        if commit is None:
            continue
        mb = _merge_base(root, "HEAD", commit) or commit
        if mb == head:
            return mb, f"{cand} (uncommitted changes on top of it)", notes
        return mb, f"{cand} (merge-base {mb[:8]})", notes
    notes.append(
        "No main or master branch was found, so only uncommitted changes are shown. "
        "Pass --base REF to compare with another branch."
    )
    return head, "HEAD (uncommitted changes only)", notes


def parse_diff(text: str) -> tuple[dict[str, set[int]], dict[str, set[int]], set[str]]:
    """Parse ``git diff -U0`` output.

    Returns (added/changed lines per new path, deletion anchors per new path, deleted files).
    A deletion anchor is the new-file line just before removed lines.
    """
    changed: dict[str, set[int]] = {}
    anchors: dict[str, set[int]] = {}
    deleted_files: set[str] = set()
    old_path: str | None = None
    path: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            old = line[4:].strip().strip('"')
            old_path = old[2:] if old.startswith("a/") else None
            continue
        if line.startswith("+++ "):
            new = line[4:].strip().strip('"')
            if new == "/dev/null":
                path = None
                if old_path:
                    deleted_files.add(old_path)
            else:
                path = new[2:] if new.startswith("b/") else new
                changed.setdefault(path, set())
            continue
        m = _HUNK.match(line)
        if m and path is not None:
            start = int(m.group(3))
            count = int(m.group(4)) if m.group(4) is not None else 1
            removed = int(m.group(2)) if m.group(2) is not None else 1
            if count:
                changed[path].update(range(start, start + count))
            elif removed:
                anchors.setdefault(path, set()).add(start)
    return changed, anchors, deleted_files


def _base_source(root: Path, commit: str, rel: str) -> str | None:
    code, out = _git(root, "show", f"{commit}:./{rel}")
    return out if code == 0 else None


# ---------------------------------------------------------------------------------------------
# changed functions


def change_set(root: Path, cfg: Config, *, base_ref: str | None = None) -> ChangeSet:
    root = Path(root).resolve()
    cs = ChangeSet(base_ref=base_ref)
    if not in_git_repo(root):
        cs.git = False
        cs.label = "no git: every public function"
        cs.notes.append(
            "This folder is not a git repository (or git is not installed), so lawkeep cannot "
            "tell what changed. It checked every public function in your source instead."
        )
        index = sm.build_index(root, cfg)
        for name, fn in sorted(index.by_name.items()):
            if fn.is_public:
                cs.functions.append(ChangedFunction(name, fn.file, []))
        return cs
    commit, label, notes = resolve_base(root, base_ref)
    cs.base_commit, cs.label = commit, label
    cs.notes.extend(notes)
    if cs.base_ref is None and commit is not None:
        cs.base_ref = label.split(" ", 1)[0]
    changed: dict[str, set[int]] = {}
    anchors: dict[str, set[int]] = {}
    deleted_files: set[str] = set()
    if commit is not None:
        code, out = _git(
            root, "diff", "--relative", "-U0", "--no-color", "--no-ext-diff", "-M", commit, "--"
        )
        if code != 0:
            cs.notes.append("git diff failed, so no changes could be read.")
        changed, anchors, deleted_files = parse_diff(out)
        code, out = _git(root, "ls-files", "--others", "--exclude-standard")
        new_files = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []
    else:
        code, out = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
        new_files = [ln for ln in out.splitlines() if ln.strip()] if code == 0 else []
    for rel in new_files:
        p = root / rel
        if p.suffix == ".py" and p.is_file():
            try:
                n = len(p.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                continue
            changed[rel] = set(range(1, n + 1))

    index_files = [root / rel for rel in changed if sm.is_source_file(root, cfg, root / rel)]
    index = sm.build_index(root, cfg, files=[p for p in index_files if p.is_file()])
    for rel in sorted(changed):
        p = root / rel
        if not p.is_file() or not sm.is_source_file(root, cfg, p):
            continue
        rel_key = paths.rel(root, p)
        funcs = index.by_file.get(rel_key, [])
        per_fn: dict[str, set[int]] = {}
        outside: set[int] = set()
        for line in changed[rel]:
            fn = sm.function_at(funcs, line)
            if fn is None:
                outside.add(line)
            else:
                per_fn.setdefault(fn.qualname, set()).add(line)
        for a in anchors.get(rel, set()):
            fa, fb = sm.function_at(funcs, a), sm.function_at(funcs, a + 1)
            if fa is not None and fb is not None and fa.qualname == fb.qualname:
                per_fn.setdefault(fa.qualname, set()).add(a)
        for fn in funcs:
            if fn.qualname in per_fn:
                cs.functions.append(
                    ChangedFunction(
                        index.name_of(rel_key, fn.qualname), rel_key, sorted(per_fn[fn.qualname])
                    )
                )
        # whole-line blanks/comments outside functions are noise; keep real code lines only
        if outside:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                lines = []
            code_lines = sorted(
                n
                for n in outside
                if 0 < n <= len(lines)
                and lines[n - 1].strip()
                and not lines[n - 1].strip().startswith("#")
            )
            if code_lines:
                cs.module_level[rel_key] = code_lines
        if commit is not None:
            old = _base_source(root, commit, rel)
            if old is not None:
                now = {f.qualname for f in funcs}
                for f in sm.functions_in_source(old, rel_key):
                    if f.qualname not in now:
                        cs.deleted.append(sm.display_name(root, cfg, rel_key, f.qualname))
    for rel in sorted(deleted_files):
        if commit is None or not sm.is_source_file(root, cfg, root / rel):
            continue
        old = _base_source(root, commit, rel)
        if old is not None:
            for f in sm.functions_in_source(old, rel):
                cs.deleted.append(sm.display_name(root, cfg, rel, f.qualname))
    return cs


def changed_functions(
    root: Path, cfg: Config, *, base_ref: str | None = None
) -> list[ChangedFunction]:
    """Functions whose lines changed since the base (new files count entirely)."""
    return change_set(root, cfg, base_ref=base_ref).functions


# ---------------------------------------------------------------------------------------------
# the report


def protection_report(root: Path, cfg: Config, *, base_ref: str | None = None) -> ProtectionReport:
    """Plant bugs only in changed functions and label each one protected / partly / not.

    Informational: the result code is always 0 (lawkeep shows; it never blocks by itself).
    Raises SetupError only when an explicit ``base_ref`` does not exist.
    """
    from lawkeep.strength.core import catch_matrix, law_targets

    root = Path(root).resolve()
    cs = change_set(root, cfg, base_ref=base_ref)
    report = ProtectionReport(base_ref=cs.base_ref)
    report.notes.extend(cs.notes)
    if not cs.git:
        report.base_ref = None
    for name in cs.deleted:
        report.notes.append(
            f"{sm.short(name)} ({name}) was removed; there is nothing left to protect."
        )
    for rel, lines in cs.module_level.items():
        shown = ", ".join(str(n) for n in lines[:8]) + (" ..." if len(lines) > 8 else "")
        report.notes.append(
            f"{rel}: {len(lines)} changed line(s) outside any function (line {shown}); "
            f"this report does not cover module-level code, so read those lines."
        )
    if not cs.functions:
        report.summary = f"No changed functions to check since {cs.label}."
        report.exit_code = exitcodes.ALL_HOLD
        return report

    names = [c.function for c in cs.functions]
    limit = MAX_BUGS_PER_FUNCTION if cs.git else MAX_BUGS_NO_GIT
    targets = law_targets(root)
    matrix = catch_matrix(root, cfg, targets, functions=names, max_bugs=limit)
    report.notes.extend(n for n in matrix.notes if n not in report.notes)
    if not targets:
        report.notes.append(
            "There are no laws yet, so nothing is protected. Add laws with `lawkeep draft`."
        )
    failing = set(matrix.failing_laws)
    for cf in cs.functions:
        bugs = [b for b in matrix.bugs if b.function == cf.function]
        ids = {b.id for b in bugs}
        laws = [k for k in matrix.laws_by_function.get(cf.function, []) if k not in failing]
        caught_ids: set[str] = set()
        for k in laws:
            caught_ids.update(i for i in matrix.caught.get(k, []) if i in ids)
        total, caught = len(bugs), len(caught_ids)
        note = None
        if not laws:
            status = "not_protected"
            note = "no law runs this function - read this one"
            if matrix.laws_by_function.get(cf.function):
                note = "only laws that fail today run this function - read this one"
        elif total == 0:
            status = "partly"
            note = "a law runs this function, but lawkeep could not plant bugs in it - read it"
        elif caught == total:
            status = "protected"
        elif caught == 0:
            status = "not_protected"
            note = "laws run this function but catch none of the planted bugs - read this one"
        else:
            status = "partly"
            missed = [b for b in bugs if b.id not in caught_ids]
            shown = "; ".join(b.plain() for b in missed[:3])
            more = f" (and {len(missed) - 3} more)" if len(missed) > 3 else ""
            note = f"missed: {shown}{more}"
        report.functions.append(
            ProtectionResult(
                function=cf.function,
                file=cf.file,
                status=status,
                changed_lines=cf.changed_lines,
                planted_bugs_total=total,
                planted_bugs_caught=caught,
                laws=laws,
                note=note,
            )
        )
    if any(f.status == "partly" for f in report.functions):
        report.notes.append(
            "A few planted bugs may not change what the code does (for example `>` vs `>=` "
            "where both sides are equal), so a missed bug is worth a look, not proof of a gap."
        )
    counts = {
        s: sum(1 for f in report.functions if f.status == s)
        for s in ("protected", "partly", "not_protected")
    }
    need = counts["partly"] + counts["not_protected"]
    n = len(report.functions)
    what = f"{n} changed function(s)" if cs.git else f"{n} public function(s)"
    report.summary = (
        f"{what} since {cs.label}: {counts['protected']} protected, {counts['partly']} partly, "
        f"{counts['not_protected']} NOT protected. "
        + (
            f"Only review what your laws don't protect: {need} of {n} need a person's eyes."
            if need
            else "Every planted bug was caught by a law (evidence, not a proof)."
        )
    )
    report.exit_code = exitcodes.ALL_HOLD
    return report
