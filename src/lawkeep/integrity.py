"""Integrity check: spot law changes that did not go through lawkeep, and side doors.

Owner: workstream B (gate).

No project code runs here: files are hashed, read as text, or parsed with ``ast``. The check
only flags; it never blocks or changes anything. ``severity="error"`` means a person should
decide (``lawkeep check`` then returns at least 2); ``"warning"`` and ``"info"`` are shown only.

What is checked:

1. ``laws.lock`` vs the files now (``lock.diff_against_lock``): every changed, added or
   removed law file is an unrecorded law change (error), with the laws inside it named.
2. The append-only files (law history, saved failing examples) only grew
   (``lock.check_append_only``; with git, also compared with the base commit).
3. ``laws.lock`` itself is explained by the history (``history.unexplained_lock_paths``);
   a lock rewritten by hand is flagged. An unreadable history is flagged, not a crash.
4. Static discovery problems (sentence not a plain string, duplicate law ids): warnings.
5. Side doors (``scan_side_doors``): conftest.py outside laws/, sitecustomize.py,
   usercustomize.py, ``*.pth``, bytecode that differs from its source or has no source, hooks
   in laws/conftest.py, skip/xfail markers and settings that try fewer examples in laws/.
6. Code under test that reaches for the law runner (``scan_source``): the project's source
   folders importing lawkeep, pytest or Hypothesis, reading ``LAWKEEP_*`` variables, raising
   skips, defining comparisons that always say "equal", or modules that shadow the standard
   library or an installed package. The gate runs the code under test in the same process
   as the laws, so such code could change results; it is flagged, not prevented.
7. Approvals added since the git base that were recorded without a terminal
   (``approval_unconfirmed``, a warning): lawkeep records who claimed to approve, it cannot
   confirm that a person did. Approvals a person later confirmed or undid in a terminal
   (``confirm_approval`` / ``undo_approval``, see ``approvals.py``) are not flagged.

Issue kinds added by B beyond docs/ARCHITECTURE.md: ``lock_unreadable``, ``git_unavailable``
(info), ``weakened_settings`` (fewer examples / phases / profiles in laws/),
``laws_conftest_hook`` (hooks in laws/conftest.py, ignored by the gate),
``site_packages_side_door`` (sitecustomize/usercustomize/.pth with code in the interpreter's
site-packages changed after laws.lock was written; a warning, because the gate starts Python
with ``-S`` and never runs them; well-known files such as ``_virtualenv.pth`` are skipped),
``source_side_door`` (see 6), ``approval_unconfirmed`` (see 7).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import marshal
import os
import re
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType
from typing import Any

from lawkeep import discovery, history, lock, paths
from lawkeep.config import Config
from lawkeep.errors import HistoryError, LockError
from lawkeep.models import IntegrityIssue, as_dict, as_list, parse_iso

#: Folders never scanned for side doors (environments, VCS data, caches of other tools).
SCAN_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        ".tox",
        ".nox",
        "node_modules",
        "site-packages",
        "dist-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        ".lawkeep",
        "build",
        "dist",
    }
)
_MAX_SCANNED_FILES = 20000
_SKIP_MARK_NAMES = frozenset({"skip", "skipif", "xfail"})
_SKIP_CALLS = {
    ("pytest", "skip"),
    ("pytest", "xfail"),
    ("pytest", "importorskip"),
    ("unittest", "skip"),
    ("unittest", "skipIf"),
    ("unittest", "skipUnless"),
    ("unittest", "expectedFailure"),
}


# ---------------------------------------------------------------------------------------------
# git helpers (all optional: no git -> None)


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _git_bytes(root: Path, *args: str) -> bytes | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, timeout=30, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


@dataclass
class GitView:
    """What git says about the working tree (None fields = unknown)."""

    toplevel: Path | None = None
    base: str | None = None  # the ref compared with ("HEAD" by default)
    changed: set[str] = field(default_factory=set[str])  # root-relative paths changed or new
    note: str | None = None

    @property
    def available(self) -> bool:
        return self.toplevel is not None


def git_view(root: Path, base_ref: str | None = None) -> GitView:
    root = Path(root).resolve()
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return GitView(note="no git repository here (or git is not installed)")
    toplevel = Path(top.strip()).resolve()
    view = GitView(toplevel=toplevel)
    base = base_ref or "HEAD"
    has_base = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}") is not None
    if base_ref and not has_base:
        view.note = f"the git ref {base_ref!r} was not found, so every file was treated as new"
    names: list[str] = []
    if has_base:
        out = _git(root, "diff", "--name-only", "--no-renames", base, "--")
        names += (out or "").splitlines()
        view.base = base
    else:
        out = _git(root, "ls-files")
        names += (out or "").splitlines()
    out = _git(root, "ls-files", "--others", "--exclude-standard")
    names += (out or "").splitlines()
    for name in names:
        if not name.strip():
            continue
        r = paths.rel(root, toplevel / name)
        if not r.startswith("/"):
            view.changed.add(r)
    return view


# ---------------------------------------------------------------------------------------------
# lock / history checks


def _lock_issues(root: Path, cfg: Config) -> tuple[list[IntegrityIssue], set[str]]:
    """Issues from laws.lock / history, plus the set of files that differ from the lock."""
    issues: list[IntegrityIssue] = []
    try:
        locked = lock.read_lock(root)
    except LockError as exc:
        issues.append(
            IntegrityIssue(
                kind="lock_unreadable",
                message=f"laws.lock cannot be read: {exc.message} lawkeep cannot tell which "
                f"law changes were recorded until it is restored.",
                path=paths.LOCK_FILENAME,
                details={"hint": exc.hint},
            )
        )
        return issues, set()
    current_fp = lock.fingerprint(root)
    static = discovery.discover_laws(root)
    laws_by_file: dict[str, list[str]] = {}
    current_hash: dict[str, tuple[str | None, str | None, str]] = {}
    for info in static.laws:
        laws_by_file.setdefault(info.file, []).append(info.law_id)
        current_hash[info.law_id] = (info.source_hash, info.sentence, info.file)

    if locked is None:
        law_files = [p for p in current_fp if p.startswith(f"{paths.LAWS_DIRNAME}/")]
        if law_files or static.laws:
            issues.append(
                IntegrityIssue(
                    kind="lock_missing",
                    message=(
                        "laws.lock is missing, so lawkeep cannot tell whether the laws were "
                        f"changed outside lawkeep ({len(law_files)} file(s) in laws/). If this "
                        "is a new project, run `lawkeep init`; otherwise restore laws.lock from "
                        "git."
                    ),
                    path=paths.LOCK_FILENAME,
                )
            )
        else:
            issues.append(
                IntegrityIssue(
                    kind="lock_missing",
                    message="laws.lock does not exist yet; run `lawkeep init` to set up lawkeep.",
                    path=paths.LOCK_FILENAME,
                    severity="info",
                )
            )
        return issues, set(current_fp)

    diff = lock.compare(locked, current_fp)
    unrecorded = set(diff.paths())
    for path in diff.changed:
        issues.append(_changed_file_issue(path, locked, current_hash, laws_by_file))
    for path in diff.added:
        now = sorted(laws_by_file.get(path, []))
        what = f" It holds law(s): {', '.join(now)}." if now else ""
        issues.append(
            IntegrityIssue(
                kind="unrecorded_law_added",
                message=(
                    f"{path} was added to the laws directly, without lawkeep (an unrecorded law "
                    f"change).{what} What to do: if you want it, a person accepts it with "
                    f"`lawkeep approve --accept-unrecorded`; otherwise delete the file."
                ),
                path=path,
                details={"laws": now},
            )
        )
    for path in diff.removed:
        before = sorted(i.law_id for i in locked.laws.values() if i.file == path)
        what = f" It held law(s): {', '.join(before)}." if before else ""
        issues.append(
            IntegrityIssue(
                kind="unrecorded_law_removed",
                message=(
                    f"{path} was deleted directly, without lawkeep (an unrecorded law change)."
                    f"{what} Deleting a law weakens it, so it needs a person's yes. What to do: "
                    f"put the file back with git, or a person accepts the removal with "
                    f"`lawkeep approve --accept-unrecorded`."
                ),
                path=path,
                details={"laws": before},
            )
        )

    for problem in lock.check_append_only(root, locked):
        issues.append(
            IntegrityIssue(
                kind="append_only_violation",
                message=(
                    f"{problem.message} Restore the file from git; saved failing examples and "
                    f"the law history are kept forever."
                ),
                path=problem.path,
            )
        )

    try:
        entries = history.read(root)
    except HistoryError as exc:
        issues.append(
            IntegrityIssue(
                kind="history_unreadable",
                message=f"The law history cannot be read: {exc.message}",
                path=paths.rel(root, paths.history_path(root)),
                details={"hint": exc.hint},
            )
        )
    else:
        bad = history.unexplained_lock_paths(locked, entries)
        if bad:
            shown = ", ".join(bad[:8]) + (" ..." if len(bad) > 8 else "")
            issues.append(
                IntegrityIssue(
                    kind="lock_rewritten",
                    message=(
                        f"laws.lock was rewritten outside lawkeep: its fingerprints for {shown} "
                        f"do not match the law history. Someone may have changed a law and "
                        f"regenerated the lock by hand. Restore laws.lock from git, or have a "
                        f"person accept the change."
                    ),
                    path=paths.LOCK_FILENAME,
                    details={"paths": bad},
                )
            )

    for problem in static.problems:
        issues.append(IntegrityIssue(kind="discovery_problem", message=problem, severity="warning"))
    return issues, unrecorded


def _changed_file_issue(
    path: str,
    locked: lock.LockFile,
    current_hash: dict[str, tuple[str | None, str | None, str]],
    laws_by_file: dict[str, list[str]],
) -> IntegrityIssue:
    if path == paths.CONFIG_FILENAME:
        return IntegrityIssue(
            kind="unrecorded_law_change",
            message=(
                "lawkeep.toml (the settings) was changed directly, without lawkeep (an "
                "unrecorded law change). Settings can weaken every law at once (for example "
                "fewer inputs per law). What to do: a person looks at the change and accepts it "
                "with `lawkeep approve --accept-unrecorded`, or puts the file back with git."
            ),
            path=path,
        )
    changed_laws: list[str] = []
    sentence_changes: list[str] = []
    for law_id, info in sorted(locked.laws.items()):
        if info.file != path:
            continue
        now = current_hash.get(law_id)
        if now is None:
            changed_laws.append(f"{law_id} (removed)")
        elif now[0] != info.hash:
            changed_laws.append(law_id)
            if now[1] != info.sentence:
                sentence_changes.append(f"{law_id}: “{info.sentence}” → “{now[1]}”")
    for law_id in sorted(laws_by_file.get(path, [])):
        if law_id not in locked.laws:
            changed_laws.append(f"{law_id} (new)")
    if changed_laws:
        what = f" Laws affected: {', '.join(changed_laws)}."
    elif path.endswith("strategies.py") or path.endswith("conftest.py"):
        what = (
            " This file holds input generators or fixtures, so it can change what every law checks."
        )
    else:
        what = " No law's own code changed, but helpers in this file can change what laws check."
    if sentence_changes:
        what += " Sentence changed: " + "; ".join(sentence_changes) + "."
    details = {"laws": changed_laws, "sentence_changes": sentence_changes}
    return IntegrityIssue(
        kind="unrecorded_law_change",
        message=(
            f"{path} was edited directly, without lawkeep (an unrecorded law change).{what} "
            f"Law changes must go through lawkeep (`lawkeep propose`). What to do: put the file "
            f"back with git, or a person accepts the edit with "
            f"`lawkeep approve --accept-unrecorded`."
        ),
        path=path,
        details=details,
    )


def _git_append_only_issues(root: Path, view: GitView) -> list[IntegrityIssue]:
    """The append-only files must start with their content at the git base."""
    issues: list[IntegrityIssue] = []
    if not view.available or not view.base or view.toplevel is None:
        return issues
    for path in (paths.history_path(root), paths.ledger_path(root)):
        relpath = paths.rel(root, path)
        try:
            in_git = Path(path).resolve().relative_to(view.toplevel).as_posix()
        except ValueError:
            continue
        old = _git_bytes(root, "show", f"{view.base}:{in_git}")
        if old is None:
            continue
        new = path.read_bytes() if path.is_file() else b""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        if [ln.rstrip(b"\r\n") for ln in new_lines[: len(old_lines)]] != [
            ln.rstrip(b"\r\n") for ln in old_lines
        ]:
            issues.append(
                IntegrityIssue(
                    kind="append_only_violation",
                    message=(
                        f"{relpath} lost or changed lines compared with git {view.base} "
                        f"({len(old_lines)} line(s) there, {len(new_lines)} now). Entries may "
                        f"only be added, never removed or edited. Restore it with "
                        f"`git checkout {view.base} -- {relpath}` and add new lines after."
                    ),
                    path=relpath,
                    details={"base": view.base},
                )
            )
    return issues


# ---------------------------------------------------------------------------------------------
# Side doors


def _iter_tree(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SCAN_SKIP_DIRS
            and not d.endswith(".egg-info")
            and not (here / d / "pyvenv.cfg").is_file()
        )
        for name in sorted(filenames):
            count += 1
            if count > _MAX_SCANNED_FILES:
                return
            yield here / name


def _in_laws(relpath: str) -> bool:
    return relpath.startswith(f"{paths.LAWS_DIRNAME}/") and not relpath.startswith(
        f"{paths.LAWS_DIRNAME}/{paths.DRAFTS_DIRNAME}/"
    )


def _code_equal(a: CodeType, b: CodeType) -> bool:
    for attr in (
        "co_code",
        "co_names",
        "co_varnames",
        "co_freevars",
        "co_cellvars",
        "co_argcount",
        "co_kwonlyargcount",
        "co_posonlyargcount",
        "co_flags",
        "co_name",
    ):
        if getattr(a, attr, None) != getattr(b, attr, None):
            return False
    ca, cb = a.co_consts, b.co_consts
    if len(ca) != len(cb):
        return False
    for x, y in zip(ca, cb, strict=True):
        if isinstance(x, CodeType) and isinstance(y, CodeType):
            if not _code_equal(x, y):
                return False
        elif type(x) is not type(y) or x != y:
            if not (x != x and y != y):  # NaN constants
                return False
    return True


def check_bytecode(pyc: Path) -> str | None:
    """Why ``pyc`` (inside a __pycache__ folder) is suspicious, or None.

    Only bytecode Python would actually use is compared: same Python version, header valid
    for the current source (timestamp+size, or source hash). Then the code inside is compared
    with a fresh compile of the source; a difference means the .pyc was patched. pytest's own
    rewritten bytecode (``*-pytest-*.pyc``) is skipped because it differs from the source on
    purpose.
    """
    name = pyc.name
    tag = sys.implementation.cache_tag
    if not tag or f".{tag}." not in name or "-pytest-" in name:
        return None
    try:
        source = Path(importlib.util.source_from_cache(str(pyc)))
    except ValueError:
        return None
    try:
        data = pyc.read_bytes()
    except OSError:
        return None
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
        return None
    if not source.is_file():
        return "orphan"
    try:
        src_bytes = source.read_bytes()
        st = source.stat()
    except OSError:
        return None
    flags = int.from_bytes(data[4:8], "little")
    if flags == 0:
        mtime = int.from_bytes(data[8:12], "little")
        size = int.from_bytes(data[12:16], "little")
        if mtime != (int(st.st_mtime) & 0xFFFFFFFF) or size != (st.st_size & 0xFFFFFFFF):
            return None  # stale: Python recompiles it, so it is harmless
    else:
        check_source = bool(flags & 0b10)
        if check_source and data[8:16] != importlib.util.source_hash(src_bytes):
            return None
    opt = 0
    if ".opt-1." in name:
        opt = 1
    elif ".opt-2." in name:
        opt = 2
    try:
        cached = marshal.loads(data[16:])
        fresh = compile(src_bytes, str(source), "exec", dont_inherit=True, optimize=opt)
    except Exception:
        return "unreadable"
    if not isinstance(cached, CodeType):
        return "unreadable"
    return None if _code_equal(cached, fresh) else "mismatch"


def _skip_markers(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, description) for skip/xfail markers and calls in a law file."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _SKIP_MARK_NAMES:
            # pytest.mark.skip / mark.xfail / pytest.mark.skipif(...)
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "mark":
                found.append((node.lineno, f"pytest.mark.{node.attr}"))
            elif isinstance(value, ast.Name) and value.id == "mark":
                found.append((node.lineno, f"mark.{node.attr}"))
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                if (f.value.id, f.attr) in _SKIP_CALLS:
                    found.append((node.lineno, f"{f.value.id}.{f.attr}(...)"))
                elif f.attr == "skipTest":
                    found.append((node.lineno, f"{f.value.id}.skipTest(...)"))
            elif isinstance(f, ast.Name) and f.id in ("skip", "xfail", "importorskip"):
                found.append((node.lineno, f"{f.id}(...)"))
        elif isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            name = (
                exc.attr
                if isinstance(exc, ast.Attribute)
                else exc.id
                if isinstance(exc, ast.Name)
                else ""
            )
            if name in ("SkipTest", "Skipped"):
                found.append((node.lineno, f"raise {name}"))
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for item in sorted(found):
        if item[0] not in seen:
            seen.add(item[0])
            out.append(item)
    return out


def _weakened_settings(tree: ast.AST, examples: int) -> list[tuple[int, str]]:
    """(line, description) for Hypothesis settings that try fewer cases."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        fname = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if fname in ("register_profile", "load_profile"):
            found.append(
                (node.lineno, f"settings.{fname}(...) changes Hypothesis settings for every law")
            )
            continue
        if fname == "reproduce_failure":
            found.append(
                (node.lineno, "@reproduce_failure replaces input generation with one case")
            )
            continue
        if fname != "settings":
            continue
        for kw in node.keywords:
            if kw.arg == "max_examples":
                v = kw.value
                if isinstance(v, ast.Constant) and isinstance(v.value, int):
                    if v.value < examples:
                        found.append(
                            (
                                node.lineno,
                                f"settings(max_examples={v.value}) asks for fewer examples than "
                                f"the profile's {examples}",
                            )
                        )
                else:
                    found.append((node.lineno, "settings(max_examples=...) with a computed value"))
            elif kw.arg == "phases":
                found.append((node.lineno, "settings(phases=...) can switch off input generation"))
    return found


def _site_packages_dirs() -> list[Path]:
    out: list[Path] = []
    for key in ("purelib", "platlib"):
        try:
            p = Path(sysconfig.get_paths()[key]).resolve()
        except (KeyError, OSError):
            continue
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def _lock_time(root: Path) -> float | None:
    try:
        locked = lock.read_lock(root)
    except LockError:
        return None
    if locked is None or not locked.generated_at:
        return None
    try:
        return parse_iso(locked.generated_at).timestamp()
    except ValueError:
        return None


#: ``.pth`` files that virtualenv, uv, setuptools and coverage.py write in every environment.
_BENIGN_PTH = re.compile(
    r"^(?:"
    r"import _virtualenv"
    r"|import __editable___[A-Za-z0-9_]+_finder\s*;\s*__editable___[A-Za-z0-9_]+_finder"
    r"\.install\([^\n]*\)"
    r"|import os; var = 'SETUPTOOLS_USE_DISTUTILS'.*"
    r"|import sys; exec\(.*coverage\.process_startup.*\)"
    r"|import coverage; coverage\.process_startup\(\)"
    r")\s*$"
)


def _benign_pth(text: str) -> bool:
    code = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith(("import ", "import\t"))
    ]
    return bool(code) and all(_BENIGN_PTH.match(ln) for ln in code)


def _site_packages_issues(root: Path) -> list[IntegrityIssue]:
    """sitecustomize/usercustomize/.pth-with-code in site-packages changed after laws.lock.

    A warning only: every law-running process (``lawkeep.lawrun``) starts Python with ``-S``
    and ``_boot.py``, so these files never run there, but other Python runs (your own
    tests, the app) do run them. Files every virtualenv has are not reported.
    """
    since = _lock_time(root)
    if since is None:
        return []
    issues: list[IntegrityIssue] = []
    for sp in _site_packages_dirs():
        try:
            entries = list(sp.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file():
                continue
            name = p.name
            runs_code = name in ("sitecustomize.py", "usercustomize.py")
            if not runs_code and name.endswith(".pth"):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                runs_code = any(ln.startswith(("import ", "import\t")) for ln in text.splitlines())
                if runs_code and _benign_pth(text):
                    continue
            if not runs_code:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime <= since:
                continue
            issues.append(
                IntegrityIssue(
                    kind="site_packages_side_door",
                    message=(
                        f"{p} runs code every time Python starts and was changed after "
                        f"laws.lock was last written. The gate starts Python without it, so it "
                        f"cannot change this result, but your own test runs and app do run it. "
                        f"If you did not just install a package that adds it, look at it."
                    ),
                    path=str(p),
                    severity="warning",
                )
            )
    return issues


def scan_side_doors(
    root: Path,
    *,
    base_ref: str | None = None,
    cfg: Config | None = None,
    unrecorded: set[str] | None = None,
    view: GitView | None = None,
) -> list[IntegrityIssue]:
    """conftest.py outside laws/, sitecustomize.py, usercustomize.py, .pth, stray .pyc,
    skip/xfail markers in laws/ (git diff against base_ref when given, else the working tree).

    With git, conftest.py files outside laws/ are flagged only when new or changed since the
    base (default HEAD). Without git, they are listed as info. Skip markers and weakened
    settings in laws/ are errors when their file differs from laws.lock (added silently),
    otherwise warnings (recorded, still shown).
    """
    root = Path(root).resolve()
    if view is None:
        view = git_view(root, base_ref)
    if unrecorded is None:
        try:
            unrecorded = set(lock.diff_against_lock(root).paths())
        except LockError:
            unrecorded = set[str]()
    examples = 200
    if cfg is not None:
        try:
            examples = cfg.check.get_profile().examples
        except Exception:
            pass
    issues: list[IntegrityIssue] = []
    if not view.available:
        issues.append(
            IntegrityIssue(
                kind="git_unavailable",
                message=(
                    "This folder is not a git repository (or git is not installed), so lawkeep "
                    "scanned every file for side doors instead of only changed ones and could "
                    "not compare the saved failing examples with an earlier commit."
                ),
                severity="info",
            )
        )
    elif view.note:
        issues.append(
            IntegrityIssue(kind="git_unavailable", message=f"Note: {view.note}.", severity="info")
        )

    def changed(relpath: str) -> bool | None:
        return (relpath in view.changed) if view.available else None

    for p in _iter_tree(root):
        relpath = paths.rel(root, p)
        name = p.name
        if name == "conftest.py" and not _in_laws(relpath):
            if relpath.startswith(f"{paths.LAWS_DIRNAME}/{paths.DRAFTS_DIRNAME}/"):
                continue
            ch = changed(relpath)
            if ch is False:
                continue
            issues.append(
                IntegrityIssue(
                    kind="side_door_file",
                    message=(
                        f"{relpath} is a conftest.py outside laws/"
                        + (" that is new or changed" if ch else "")
                        + ". The gate does not load it (laws run with --noconftest), but a "
                        "normal pytest run would, and it can change how code and tests behave. "
                        "Check that it does not patch the code the laws cover."
                    ),
                    path=relpath,
                    severity="warning" if ch else "info",
                    details={"changed_since_base": ch},
                )
            )
        elif name in ("sitecustomize.py", "usercustomize.py"):
            issues.append(
                IntegrityIssue(
                    kind="side_door_file",
                    message=(
                        f"{relpath}: Python runs a {name} automatically when it is on the path at "
                        f"start-up. The gate starts Python in isolated mode so it is not loaded "
                        f"there, but other runs may load it. Projects rarely need one; check why "
                        f"it is here."
                    ),
                    path=relpath,
                    severity="warning",
                    details={"changed_since_base": changed(relpath)},
                )
            )
        elif name.endswith(".pth"):
            issues.append(
                IntegrityIssue(
                    kind="side_door_file",
                    message=(
                        f"{relpath} is a .pth file. If copied into site-packages it runs code "
                        f"whenever Python starts. Projects rarely keep one in their source; "
                        f"check why it is here."
                    ),
                    path=relpath,
                    severity="warning",
                    details={"changed_since_base": changed(relpath)},
                )
            )
        elif name.endswith((".pyc", ".pyo")):
            issues.extend(_bytecode_issue(root, p, relpath))
    issues.extend(_laws_static_issues(root, unrecorded, examples, view))
    issues.extend(_site_packages_issues(root))
    return issues


def _bytecode_issue(root: Path, p: Path, relpath: str) -> list[IntegrityIssue]:
    if p.parent.name != "__pycache__":
        source = p.with_suffix(".py")
        severity = "error" if not source.exists() else "warning"
        return [
            IntegrityIssue(
                kind="bytecode_file",
                message=(
                    f"{relpath} is a compiled .pyc file outside __pycache__"
                    + (" with no matching .py source" if not source.exists() else "")
                    + ". Python can import it in place of source code, so it can hide code "
                    "nobody reviewed. Delete it unless you know why it is there."
                ),
                path=relpath,
                severity=severity,
            )
        ]
    why = check_bytecode(p)
    if why == "mismatch":
        return [
            IntegrityIssue(
                kind="bytecode_file",
                message=(
                    f"{relpath} is cached bytecode that does not match its source file, although "
                    f"its header says it does: it looks patched. The gate ignores cached "
                    f"bytecode, but a normal Python run would execute the patched code. Delete "
                    f"the __pycache__ folder and find out who wrote it."
                ),
                path=relpath,
                severity="error",
            )
        ]
    if why == "orphan" and _in_laws(relpath):
        return [
            IntegrityIssue(
                kind="bytecode_file",
                message=(
                    f"{relpath} is cached bytecode in laws/ whose source file no longer exists. "
                    f"It is not used by the gate; you can delete it."
                ),
                path=relpath,
                severity="info",
            )
        ]
    return []


def _laws_static_issues(
    root: Path, unrecorded: set[str], examples: int, view: GitView
) -> list[IntegrityIssue]:
    issues: list[IntegrityIssue] = []
    ldir = paths.laws_dir(root)
    if not ldir.is_dir():
        return issues
    for p in sorted(ldir.rglob("*.py")):
        relpath = paths.rel(root, p)
        if not _in_laws(relpath) or any(part in lock.CACHE_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=relpath)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue  # discovery reports syntax errors
        silent = relpath in unrecorded
        severity = "error" if silent else "warning"
        how = (
            "was added without going through lawkeep"
            if silent
            else "is recorded in the law history"
        )
        for line, what in _skip_markers(tree):
            issues.append(
                IntegrityIssue(
                    kind="skip_marker",
                    message=(
                        f"{relpath} line {line}: {what} can stop a law from running. This file "
                        f"{how}. Skipping a law weakens it and needs a person's yes."
                    ),
                    path=relpath,
                    severity=severity,
                    details={"line": line, "marker": what, "unrecorded": silent},
                )
            )
        for line, what in _weakened_settings(tree, examples):
            issues.append(
                IntegrityIssue(
                    kind="weakened_settings",
                    message=(
                        f"{relpath} line {line}: {what}. The gate ignores this and still uses "
                        f"the profile from lawkeep.toml, but a plain pytest run would try fewer "
                        f"cases. This file {how}."
                    ),
                    path=relpath,
                    severity=severity,
                    details={"line": line, "setting": what, "unrecorded": silent},
                )
            )
        if p.name == "conftest.py" and p.parent == ldir:
            hooks = [
                n.name
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("pytest_")
            ]
            if hooks:
                issues.append(
                    IntegrityIssue(
                        kind="laws_conftest_hook",
                        message=(
                            f"laws/conftest.py defines pytest hook(s) {', '.join(hooks)}. The "
                            f"gate loads only fixtures from it and ignores hooks, but a plain "
                            f"pytest run would let them change results."
                        ),
                        path=relpath,
                        severity=severity,
                        details={"hooks": hooks},
                    )
                )
        elif p.name == "conftest.py":
            issues.append(
                IntegrityIssue(
                    kind="side_door_file",
                    message=(
                        f"{relpath} is a conftest.py in a sub-folder of laws/. The gate only "
                        f"loads fixtures from laws/conftest.py, so this file is ignored. Move "
                        f"its fixtures to laws/conftest.py."
                    ),
                    path=relpath,
                    severity="warning",
                )
            )
    return issues


# ---------------------------------------------------------------------------------------------


def check_integrity(
    root: Path, cfg: Config, *, base_ref: str | None = None
) -> list[IntegrityIssue]:
    """All integrity issues for the project (empty list = nothing to flag). Runs no code."""
    root = Path(root).resolve()
    issues, unrecorded = _lock_issues(root, cfg)
    view = git_view(root, base_ref)
    issues.extend(_git_append_only_issues(root, view))
    issues.extend(
        scan_side_doors(root, base_ref=base_ref, cfg=cfg, unrecorded=unrecorded, view=view)
    )
    issues.extend(scan_source(root, cfg, view=view))
    issues.extend(_approval_issues(root, view))
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(issues, key=lambda i: order[i.severity])


# ---------------------------------------------------------------------------------------------
# Code under test that reaches for the law runner (no code runs: ast only)

#: Importing these from the code under test lets it change how laws run or what they report.
_RUNNER_MODULES = frozenset(
    {
        "lawkeep",
        "hypothesis",
        "pytest",
        "_pytest",
        "pluggy",
        "coverage",
        "crosshair",
        "hypothesis_crosshair_provider",
    }
)
#: Folders and files in a source folder that are tests or tooling, not code under test.
_NOT_SOURCE_DIRS = frozenset(
    {"tests", "test", "testing", "laws", "docs", "doc", "examples", "scripts", "benchmarks"}
)
_COMPARE_DUNDERS = {
    "__eq__": True,
    "__le__": True,
    "__ge__": True,
    "__lt__": True,
    "__gt__": True,
    "__ne__": False,
}
_SKIP_EXCEPTIONS = frozenset({"SkipTest", "Skipped", "OutcomeException", "UnsatisfiedAssumption"})


def _project_meta(root: Path) -> tuple[str | None, set[str]]:
    """(normalised project name, normalised names of its required dependencies)."""
    p = root / "pyproject.toml"
    if not p.is_file():
        return None, set()
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover
            import tomli as tomllib
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, set()
    proj = as_dict(data.get("project"))
    name = proj.get("name")

    def norm(n: str) -> str:
        return re.sub(r"[-_.]+", "-", n).lower()

    deps: set[str] = set()
    for d in as_list(proj.get("dependencies")):
        m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", str(d))
        if m:
            deps.add(norm(m.group(1)))
    return (norm(name) if isinstance(name, str) else None), deps


def _source_files(root: Path, cfg: Config) -> list[tuple[Path, Path]]:
    """(source folder, file) for every .py file of the code under test."""
    out: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    count = 0
    for d in cfg.project.source_dirs(root):
        if not d.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(d):
            here = Path(dirpath)
            dirnames[:] = sorted(
                x
                for x in dirnames
                if x not in SCAN_SKIP_DIRS
                and x not in _NOT_SOURCE_DIRS
                and not x.endswith(".egg-info")
                and not x.startswith(".")
                and not (here / x / "pyvenv.cfg").is_file()
            )
            for name in sorted(filenames):
                if not name.endswith(".py") or name == "conftest.py":
                    continue
                if name.startswith("test_") or name.endswith("_test.py"):
                    continue
                f = here / name
                if f in seen:
                    continue
                seen.add(f)
                count += 1
                if count > _MAX_SCANNED_FILES:
                    return out
                out.append((d, f))
    return out


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _source_findings(tree: ast.AST, *, is_lawkeep: bool) -> list[tuple[int, str, str]]:
    """(line, plain-English finding, "high"|"low") for one source file."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods = [node.module]
            if node.module.split(".")[0] == "unittest" and any(
                a.name in _SKIP_EXCEPTIONS for a in node.names
            ):
                found.append(
                    (line, "imports SkipTest (a skip raised by the code turns a law off)", "high")
                )
        for m in mods:
            top = m.split(".")[0]
            if top in _RUNNER_MODULES and not (is_lawkeep and top == "lawkeep"):
                found.append((line, f"imports {m}, which runs the laws", "high"))
            elif top == "atexit":
                found.append((line, "imports atexit (code that runs after the laws finish)", "low"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and not is_lawkeep:
            if "LAWKEEP_" in node.value or "HYPOTHESIS_" in node.value:
                found.append(
                    (line, f"mentions {node.value[:40]!r} (a setting of the law runner)", "high")
                )
        if isinstance(node, ast.Attribute) and node.attr in _SKIP_EXCEPTIONS:
            found.append(
                (line, f"uses {_dotted(node)} (a skip raised by the code turns a law off)", "high")
            )
        if isinstance(node, ast.Name) and node.id in _SKIP_EXCEPTIONS:
            found.append(
                (line, f"uses {node.id} (a skip raised by the code turns a law off)", "high")
            )
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _COMPARE_DUNDERS
        ):
            want = _COMPARE_DUNDERS[node.name]
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            if returns and all(
                isinstance(r.value, ast.Constant) and r.value.value is want for r in returns
            ):
                found.append(
                    (
                        line,
                        f"defines {node.name} that always returns {want}, so every comparison "
                        f"in a law says {'equal/in range' if want else 'not different'}",
                        "high",
                    )
                )
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            literal = bool(node.args) and isinstance(node.args[0], ast.Constant)
            if name in ("__import__", "importlib.import_module", "import_module") and not literal:
                found.append((line, f"imports a module by a computed name ({name})", "low"))
            elif name in ("exec", "eval"):
                found.append((line, f"runs computed code ({name})", "low"))
            elif name in ("gc.get_objects", "gc.get_referrers"):
                found.append((line, f"walks every live object ({name})", "low"))
        if isinstance(node, ast.Attribute) and _dotted(node) == "sys.modules":
            found.append((line, "reads or changes sys.modules (the loaded modules)", "low"))
    found.sort()
    return found


def _installed_top_levels() -> dict[str, list[str]]:
    try:
        from importlib.metadata import packages_distributions

        return {k: list(v) for k, v in packages_distributions().items()}
    except Exception:  # pragma: no cover
        return {}


def _shadow_issues(
    root: Path, cfg: Config, view: GitView, project: str | None
) -> list[IntegrityIssue]:
    issues: list[IntegrityIssue] = []
    stdlib = set(sys.stdlib_module_names)
    installed: dict[str, list[str]] | None = None
    for d in cfg.project.source_dirs(root):
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in _NOT_SOURCE_DIRS or e.name in SCAN_SKIP_DIRS:
                continue
            if e.is_file() and e.suffix == ".py" and e.name != "conftest.py":
                name = e.stem
            elif e.is_dir() and (e / "__init__.py").is_file():
                name = e.name
            else:
                continue
            if name in ("__init__", "__main__", "setup", "noxfile", "manage"):
                continue
            what = None
            if name in stdlib:
                what = "a module of Python's standard library"
            else:
                if installed is None:
                    installed = _installed_top_levels()
                dists = installed.get(name) or []
                norm = {re.sub(r"[-_.]+", "-", x).lower() for x in dists}
                if dists and (project is None or project not in norm):
                    what = f"the installed package {', '.join(sorted(dists))}"
            if what is None:
                continue
            relpath = paths.rel(root, e)
            changed = (
                (relpath in view.changed or any(c.startswith(relpath + "/") for c in view.changed))
                if view.available
                else None
            )
            issues.append(
                IntegrityIssue(
                    kind="source_side_door",
                    message=(
                        f"{relpath} has the same name as {what} ({name}). The gate puts your "
                        f"source folders first on the import path, so this file would be "
                        f"loaded instead of the real {name}, inside the law runner too. Rename "
                        f"it unless you mean to replace {name}."
                    ),
                    path=relpath,
                    severity="error" if changed else "warning",
                    details={"shadows": name, "changed_since_base": changed},
                )
            )
    return issues


def scan_source(root: Path, cfg: Config, *, view: GitView | None = None) -> list[IntegrityIssue]:
    """Flag code under test that could change how laws run (see the module docstring, 6).

    The gate runs the project's code in the same Python process as the laws, so code that
    imports lawkeep/pytest/Hypothesis, reads lawkeep's settings or raises skips could change
    the result. lawkeep cannot prevent that (the code under test is trusted); it flags it.

    With git: a file new or changed since the base gets an ``error`` (a person should look),
    an unchanged one a ``warning``. Imports of a package the project itself depends on (for
    example a pytest plugin importing pytest) are only a warning. Without git: warnings.
    """
    root = Path(root).resolve()
    if view is None:
        view = git_view(root)
    project, deps = _project_meta(root)
    is_lawkeep = project == "lawkeep"
    issues: list[IntegrityIssue] = []
    per_file: dict[str, list[tuple[int, str, str]]] = {}
    for _d, f in _source_files(root, cfg):
        relpath = paths.rel(root, f)
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=relpath)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        findings = _source_findings(tree, is_lawkeep=is_lawkeep)
        if findings:
            per_file[relpath] = findings
    for relpath, findings in per_file.items():
        changed = (relpath in view.changed) if view.available else None
        high: list[tuple[int, str]] = []
        for line, what, tier in findings:
            if tier == "high":
                m = re.match(r"imports ([\w.]+)", what)
                top = m.group(1).split(".")[0] if m else None
                top = {"_pytest": "pytest", "pluggy": "pytest"}.get(top or "", top)
                if is_lawkeep or (top and (top in deps or top.replace("_", "-") in deps)):
                    tier = "low"  # the project itself depends on it (e.g. a pytest plugin)
                elif "skip" in what and deps & {"pytest", "hypothesis"}:
                    tier = "low"
            if tier == "high":
                high.append((line, what))
        low = [(ln, w) for ln, w, _tier in findings if (ln, w) not in high]
        if high:
            shown = "; ".join(f"line {ln}: {w}" for ln, w in high[:4])
            more = f" (and {len(high) - 4} more)" if len(high) > 4 else ""
            issues.append(
                IntegrityIssue(
                    kind="source_side_door",
                    message=(
                        f"{relpath} (code under test) {shown}{more}. The laws run this code "
                        f"in the same process as lawkeep, so code like this could change what "
                        f"the laws report (for example rewrite results or skip a failing "
                        f"input). Normal app code rarely needs it: check why it is here before "
                        f"trusting this result."
                        + (" It is new or changed since the git base." if changed else "")
                    ),
                    path=relpath,
                    severity="error" if changed else "warning",
                    details={"findings": [w for _, w in high], "changed_since_base": changed},
                )
            )
        if low and changed:
            shown = "; ".join(f"line {ln}: {w}" for ln, w in low[:4])
            issues.append(
                IntegrityIssue(
                    kind="source_side_door",
                    message=(
                        f"{relpath} (code under test, changed) {shown}. This can be normal, "
                        f"but it is also how code hides what it does from the laws; worth a "
                        f"look."
                    ),
                    path=relpath,
                    severity="warning",
                    details={"findings": [w for _, w in low], "changed_since_base": changed},
                )
            )
    issues.extend(_shadow_issues(root, cfg, view, project))
    return issues


# ---------------------------------------------------------------------------------------------
# Approvals that lawkeep cannot confirm


def unconfirmed_approval_issues(root: Path) -> list[IntegrityIssue]:
    """The ``approval_unconfirmed`` check on its own (used by ``lawkeep approve``)."""
    return _approval_issues(root, git_view(root))


def _approval_issues(root: Path, view: GitView) -> list[IntegrityIssue]:
    """History entries added since the git base that claim a person's yes without a terminal."""
    if not view.available or view.toplevel is None:
        return []
    path = paths.history_path(root)
    if not path.is_file():
        return []
    try:
        in_git = Path(path).resolve().relative_to(view.toplevel).as_posix()
    except ValueError:
        return []
    old = _git_bytes(root, "show", f"{view.base}:{in_git}") if view.base else None
    n_old = len(old.splitlines()) if old else 0
    all_lines = path.read_bytes().splitlines()
    lines = all_lines[n_old:]
    # approvals a person later confirmed or undid in a terminal (lawkeep.approvals)
    settled: set[str] = set()
    for raw in all_lines:
        try:
            parsed_any: object = json.loads(raw)
        except ValueError:
            continue
        d_any = as_dict(parsed_any)
        if d_any.get("action") in ("confirm_approval", "undo_approval"):
            settled.update(str(x) for x in as_list(as_dict(d_any.get("details")).get("entries")))
    unconfirmed: list[dict[str, Any]] = []
    for raw in lines:
        try:
            parsed: object = json.loads(raw)
        except ValueError:
            continue
        e = as_dict(parsed)  # not an object: no approved_by, skipped below
        who = e.get("approved_by") or ""
        if not str(who).startswith("person:"):
            continue
        if as_dict(e.get("details")).get("terminal") is True:
            continue
        if str(e.get("id")) in settled or e.get("action") in ("confirm_approval", "undo_approval"):
            continue
        unconfirmed.append(e)
    if not unconfirmed:
        return []
    shown: list[str] = []
    for e in unconfirmed[:4]:
        d = as_dict(e.get("details"))
        what = (
            e.get("law_id") or ", ".join(str(p) for p in as_list(d.get("paths"))) or e.get("action")
        )
        shown.append(
            f"{e.get('id')} {e.get('action')} {what} (approved by {e.get('approved_by')}, "
            f"via {d.get('channel') or 'unknown channel'})"
        )
    return [
        IntegrityIssue(
            kind="approval_unconfirmed",
            message=(
                f"{len(unconfirmed)} approval(s) in laws/history.jsonl were added since "
                f"{view.base or 'the start'} without a terminal: {'; '.join(shown)}"
                + (" ..." if len(unconfirmed) > 4 else "")
                + ". lawkeep records who claimed to approve; it cannot confirm a person did. "
                "Confirm or undo each one in a terminal with `lawkeep approve`."
            ),
            path=paths.rel(root, path),
            severity="warning",
            details={"entries": [e.get("id") for e in unconfirmed]},
        )
    ]
