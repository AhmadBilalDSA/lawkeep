"""Where lawkeep keeps things inside a project.

Layout (all relative to the project root)::

    lawkeep.toml                  settings and permissions        (fingerprinted)
    laws.lock                     fingerprints of every law file  (written by lawkeep)
    laws/                         approved laws + their helpers   (fingerprinted)
    laws/strategies.py            project input generators        (fingerprinted)
    laws/history.jsonl            law history, append-only        (prefix-checked, not hashed)
    laws/failing_examples.jsonl   saved failing inputs, append-only (prefix-checked, not hashed)
    laws/drafts/                  drafts and pending proposals    (ignored by gate and lock)
    .lawkeep/                     local state: session start, caches (not committed)

The project root is the nearest folder (walking up from the start folder) that contains
``lawkeep.toml``; failing that, the nearest folder that contains a ``laws/`` folder.
When no start folder is given, the environment variable ``LAWKEEP_ROOT`` (if set) is used
instead of searching from the current folder; an explicit start folder (``--root``) wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lawkeep.errors import ProjectNotFound

CONFIG_FILENAME = "lawkeep.toml"
LOCK_FILENAME = "laws.lock"
LAWS_DIRNAME = "laws"
DRAFTS_DIRNAME = "drafts"
HISTORY_FILENAME = "history.jsonl"
LEDGER_FILENAME = "failing_examples.jsonl"
STRATEGIES_FILENAME = "strategies.py"
STATE_DIRNAME = ".lawkeep"
ROOT_ENV_VAR = "LAWKEEP_ROOT"


def find_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the project root, or ``None`` when there is no lawkeep project here.

    With ``start=None``: ``LAWKEEP_ROOT`` (if set and non-empty) is returned as is; otherwise
    the search starts in the current folder. A file path is treated as its folder.
    """
    if start is None:
        env = os.environ.get(ROOT_ENV_VAR, "").strip()
        if env:
            return Path(env).expanduser().resolve()
    here = Path(start).expanduser() if start is not None else Path.cwd()
    here = here.resolve()
    if here.is_file():
        here = here.parent
    chain = [here, *here.parents]
    for folder in chain:
        if (folder / CONFIG_FILENAME).is_file():
            return folder
    for folder in chain:
        if (folder / LAWS_DIRNAME).is_dir():
            return folder
    return None


def require_root(start: str | os.PathLike[str] | None = None) -> Path:
    """Like :func:`find_root` but raises :class:`ProjectNotFound` (result code 3)."""
    root = find_root(start)
    if root is None:
        where = Path(start).resolve() if start is not None else Path.cwd()
        raise ProjectNotFound(
            f"No lawkeep project found in {where} or any folder above it "
            f"(looked for {CONFIG_FILENAME} or a {LAWS_DIRNAME}/ folder).",
            hint="Run `lawkeep init` in your project folder to set one up.",
        )
    return root


def config_path(root: Path) -> Path:
    return Path(root) / CONFIG_FILENAME


def lock_path(root: Path) -> Path:
    return Path(root) / LOCK_FILENAME


def laws_dir(root: Path) -> Path:
    return Path(root) / LAWS_DIRNAME


def drafts_dir(root: Path) -> Path:
    return laws_dir(root) / DRAFTS_DIRNAME


def history_path(root: Path) -> Path:
    return laws_dir(root) / HISTORY_FILENAME


def ledger_path(root: Path) -> Path:
    """Saved failing inputs, replayed on every run (append-only; format owned by ledger.py)."""
    return laws_dir(root) / LEDGER_FILENAME


def strategies_path(root: Path) -> Path:
    return laws_dir(root) / STRATEGIES_FILENAME


def state_dir(root: Path) -> Path:
    """Local, uncommitted state (session start time, caches). Not tamper-protected."""
    return Path(root) / STATE_DIRNAME


def session_file(root: Path) -> Path:
    """JSON file written by adapters at session start: ``{"started_at": iso, "agent": name}``."""
    return state_dir(root) / "session.json"


@dataclass(frozen=True)
class ProjectPaths:
    """All well-known paths of one project, resolved."""

    root: Path
    config: Path
    lock: Path
    laws: Path
    drafts: Path
    history: Path
    ledger: Path
    strategies: Path
    state: Path

    def to_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


def project_paths(root: str | os.PathLike[str]) -> ProjectPaths:
    root = Path(root).resolve()
    return ProjectPaths(
        root=root,
        config=config_path(root),
        lock=lock_path(root),
        laws=laws_dir(root),
        drafts=drafts_dir(root),
        history=history_path(root),
        ledger=ledger_path(root),
        strategies=strategies_path(root),
        state=state_dir(root),
    )


def rel(root: Path, path: str | os.PathLike[str]) -> str:
    """``path`` relative to ``root`` as a forward-slash string (``"laws/refunds.py"``).

    Paths outside ``root`` are returned absolute (still forward slashes).
    """
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    p = p.resolve()
    try:
        return p.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return p.as_posix()


def is_draft(root: Path, path: str | os.PathLike[str]) -> bool:
    """True when ``path`` is inside ``laws/drafts/``."""
    r = rel(root, path)
    return r == f"{LAWS_DIRNAME}/{DRAFTS_DIRNAME}" or r.startswith(
        f"{LAWS_DIRNAME}/{DRAFTS_DIRNAME}/"
    )
