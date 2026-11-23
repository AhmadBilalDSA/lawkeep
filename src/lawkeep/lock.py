"""Fingerprints of the laws: ``laws.lock``.

What is fingerprinted (sha256 of the bytes):

- every file under ``laws/`` EXCEPT ``laws/drafts/``, ``laws/history.jsonl``,
  ``laws/failing_examples.jsonl``, cache folders (``__pycache__``, ``.hypothesis``,
  ``.pytest_cache``, ``.mypy_cache``, ``.ruff_cache``) and ``*.pyc`` / ``*.pyo`` files
  (stray bytecode is a side door the integrity check reports separately);
- ``lawkeep.toml`` (settings such as examples per law can weaken every law);
- ``.github/workflows/lawkeep.yml`` when it exists (the workflow ``lawkeep init --ci``
  installs: changing it to always pass, e.g. ``|| true``, would hide every failing law on
  pull requests).

The two append-only files (history and saved failing examples) are protected differently:
the lock stores how many lines they had and a hash of those lines, so lines may be added
but a removed or edited line is detected (:func:`check_append_only`).

``laws.lock`` format (TOML)::

    # laws.lock - written by lawkeep; do not edit by hand.
    version = 1
    generated_at = "2026-11-21T10:00:00.000000Z"
    lawkeep_version = "0.1.0.dev0"

    [files]
    "lawkeep.toml" = "sha256:..."
    "laws/refunds.py" = "sha256:..."

    [laws."refunds.refund_is_capped"]
    sentence = "A refund never exceeds the original charge"
    file = "laws/refunds.py"
    hash = "sha256:..."            # the law's own source, decorators included

    [append_only."laws/history.jsonl"]
    lines = 12
    prefix_sha256 = "sha256:..."

Nothing in this module runs project code.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from lawkeep.errors import LockError
from lawkeep.models import utc_now_iso
from lawkeep.paths import (
    CONFIG_FILENAME,
    DRAFTS_DIRNAME,
    HISTORY_FILENAME,
    LAWS_DIRNAME,
    LEDGER_FILENAME,
    config_path,
    history_path,
    laws_dir,
    ledger_path,
    lock_path,
    rel,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

LOCK_VERSION = 1
CACHE_DIRS = frozenset(
    {"__pycache__", ".hypothesis", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
BYTECODE_SUFFIXES = (".pyc", ".pyo")
APPEND_ONLY_FILES = (f"{LAWS_DIRNAME}/{HISTORY_FILENAME}", f"{LAWS_DIRNAME}/{LEDGER_FILENAME}")
#: The GitHub Action workflow installed by ``lawkeep init --ci`` (fingerprinted when present).
CI_WORKFLOW = ".github/workflows/lawkeep.yml"
_HEADER = "# laws.lock - written by lawkeep; do not edit by hand.\n"


@dataclass
class LockedLaw:
    law_id: str
    sentence: str | None
    file: str
    hash: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AppendOnlyMark:
    lines: int
    prefix_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LockFile:
    files: dict[str, str] = field(default_factory=dict[str, str])  # relative path -> "sha256:<hex>"
    laws: dict[str, LockedLaw] = field(default_factory=dict[str, "LockedLaw"])
    append_only: dict[str, AppendOnlyMark] = field(default_factory=dict[str, "AppendOnlyMark"])
    version: int = LOCK_VERSION
    generated_at: str = field(default_factory=utc_now_iso)
    lawkeep_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "lawkeep_version": self.lawkeep_version,
            "files": dict(sorted(self.files.items())),
            "laws": {k: v.to_dict() for k, v in sorted(self.laws.items())},
            "append_only": {k: v.to_dict() for k, v in sorted(self.append_only.items())},
        }


@dataclass
class LockDiff:
    """Difference between two sets of fingerprints (paths relative to the project root)."""

    changed: list[str] = field(default_factory=list[str])
    added: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])

    @property
    def is_empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def paths(self) -> list[str]:
        return sorted({*self.changed, *self.added, *self.removed})

    def to_dict(self) -> dict[str, Any]:
        return {"changed": self.changed, "added": self.added, "removed": self.removed}


@dataclass
class AppendOnlyProblem:
    path: str
    message: str  # plain English

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def is_fingerprinted(root: Path, path: str | os.PathLike[str]) -> bool:
    """Would ``path`` be part of the fingerprint? (It need not exist.)"""
    r = rel(root, path)
    if r in (CONFIG_FILENAME, CI_WORKFLOW):
        return True
    if not r.startswith(f"{LAWS_DIRNAME}/"):
        return False
    parts = r.split("/")
    if len(parts) > 1 and parts[1] == DRAFTS_DIRNAME:
        return False
    if r in APPEND_ONLY_FILES:
        return False
    if any(p in CACHE_DIRS for p in parts):
        return False
    return not r.endswith(BYTECODE_SUFFIXES)


def iter_fingerprinted_files(root: str | os.PathLike[str]) -> list[Path]:
    """Every existing file that is part of the fingerprint, sorted by relative path."""
    root = Path(root).resolve()
    out: list[Path] = []
    cfg = config_path(root)
    if cfg.is_file():
        out.append(cfg)
    wf = root / CI_WORKFLOW
    if wf.is_file():
        out.append(wf)
    ldir = laws_dir(root)
    if ldir.is_dir():
        for dirpath, dirnames, filenames in os.walk(ldir):
            here = Path(dirpath)
            keep: list[str] = []
            for d in dirnames:
                if d in CACHE_DIRS:
                    continue
                if here == ldir and d == DRAFTS_DIRNAME:
                    continue
                keep.append(d)
            dirnames[:] = sorted(keep)
            for name in sorted(filenames):
                p = here / name
                if p.is_file() and is_fingerprinted(root, p):
                    out.append(p)
    return sorted(out, key=lambda p: rel(root, p))


def fingerprint(root: str | os.PathLike[str]) -> dict[str, str]:
    """``{relative path: "sha256:<hex>"}`` for every fingerprinted file."""
    root = Path(root).resolve()
    return {rel(root, p): hash_file(p) for p in iter_fingerprinted_files(root)}


def append_only_mark(path: str | os.PathLike[str]) -> AppendOnlyMark:
    """Line count and hash of the whole file (a missing file is 0 lines)."""
    p = Path(path)
    data = p.read_bytes() if p.is_file() else b""
    lines = data.splitlines(keepends=True)
    return AppendOnlyMark(lines=len(lines), prefix_sha256=sha256_bytes(b"".join(lines)))


def build_lock(root: str | os.PathLike[str]) -> LockFile:
    """Fingerprint the project now (files, per-law sentences and hashes, append-only marks)."""
    from lawkeep import __version__
    from lawkeep.discovery import discover_laws

    root = Path(root).resolve()
    lock = LockFile(files=fingerprint(root), lawkeep_version=__version__)
    for info in discover_laws(root).laws:
        lock.laws.setdefault(
            info.law_id,
            LockedLaw(
                law_id=info.law_id, sentence=info.sentence, file=info.file, hash=info.source_hash
            ),
        )
    for path in (history_path(root), ledger_path(root)):
        lock.append_only[rel(root, path)] = append_only_mark(path)
    return lock


def dumps_lock(lock: LockFile) -> str:
    d = lock.to_dict()
    laws: dict[str, dict[str, Any]] = {}
    for law_id, info in d["laws"].items():
        laws[law_id] = {k: v for k, v in info.items() if k != "law_id" and v is not None}
    doc = {
        "version": d["version"],
        "generated_at": d["generated_at"],
        "lawkeep_version": d["lawkeep_version"],
        "files": d["files"],
        "laws": laws,
        "append_only": d["append_only"],
    }
    return _HEADER + tomli_w.dumps(doc)


def loads_lock(text: str, *, source: str = "laws.lock") -> LockFile:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LockError(
            f"{source} is not valid TOML ({exc}). It may have been edited by hand or left with "
            f"merge-conflict markers.",
            hint="Restore it from git, or ask a person to re-create it with lawkeep.",
        ) from None
    try:
        version = int(data.get("version", 0))
        if version != LOCK_VERSION:
            raise LockError(
                f"{source} has version {version}, but this lawkeep reads version {LOCK_VERSION}.",
                hint="Upgrade lawkeep, or restore laws.lock from git.",
            )
        files = {str(k): str(v) for k, v in dict(data.get("files", {})).items()}
        laws = {
            str(k): LockedLaw(
                law_id=str(k),
                sentence=v.get("sentence"),
                file=str(v.get("file", "")),
                hash=v.get("hash"),
            )
            for k, v in dict(data.get("laws", {})).items()
        }
        append_only = {
            str(k): AppendOnlyMark(lines=int(v["lines"]), prefix_sha256=str(v["prefix_sha256"]))
            for k, v in dict(data.get("append_only", {})).items()
        }
    except LockError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise LockError(f"{source} has an unexpected shape ({exc}).") from None
    return LockFile(
        files=files,
        laws=laws,
        append_only=append_only,
        version=version,
        generated_at=str(data.get("generated_at", "")),
        lawkeep_version=str(data.get("lawkeep_version", "")),
    )


def read_lock(root: str | os.PathLike[str]) -> LockFile | None:
    """Read ``laws.lock``; ``None`` when it does not exist. Raises LockError when unreadable."""
    path = lock_path(Path(root))
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LockError(f"Cannot read {path}: {exc}") from None
    return loads_lock(text, source=str(path.name))


def write_lock(root: str | os.PathLike[str], lock: LockFile) -> Path:
    """Write ``laws.lock`` atomically (temp file + rename). Returns its path."""
    path = lock_path(Path(root))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps_lock(lock), encoding="utf-8")
    os.replace(tmp, path)
    return path


def compare(
    old: LockFile | Mapping[str, str] | None, new: LockFile | Mapping[str, str]
) -> LockDiff:
    """Which fingerprinted files changed, were added or were removed from ``old`` to ``new``."""
    old_files = old.files if isinstance(old, LockFile) else dict(old or {})
    new_files = new.files if isinstance(new, LockFile) else dict(new)
    diff = LockDiff()
    for path in sorted(set(old_files) | set(new_files)):
        if path not in new_files:
            diff.removed.append(path)
        elif path not in old_files:
            diff.added.append(path)
        elif old_files[path] != new_files[path]:
            diff.changed.append(path)
    return diff


def diff_against_lock(root: str | os.PathLike[str]) -> LockDiff:
    """Files that differ from ``laws.lock`` right now (all files count as added if no lock)."""
    return compare(read_lock(root), fingerprint(root))


def check_append_only(
    root: str | os.PathLike[str], lock: LockFile | None
) -> list[AppendOnlyProblem]:
    """Report append-only files whose locked lines were removed or edited."""
    if lock is None:
        return []
    root = Path(root)
    problems: list[AppendOnlyProblem] = []
    for relpath, mark in sorted(lock.append_only.items()):
        p = root / relpath
        data = p.read_bytes() if p.is_file() else b""
        lines = data.splitlines(keepends=True)
        if len(lines) < mark.lines:
            problems.append(
                AppendOnlyProblem(
                    relpath,
                    f"{relpath} lost lines: laws.lock recorded {mark.lines} line(s), now there are "
                    f"{len(lines)}. Entries may only be added, never removed.",
                )
            )
            continue
        if sha256_bytes(b"".join(lines[: mark.lines])) != mark.prefix_sha256:
            problems.append(
                AppendOnlyProblem(
                    relpath,
                    f"{relpath} was edited: one of its first {mark.lines} recorded line(s) "
                    f"changed. Entries may only be added, never changed.",
                )
            )
    return problems
