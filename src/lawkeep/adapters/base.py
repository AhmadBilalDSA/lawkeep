"""Shared adapter base: what every agent gets (CLI, MCP server, AGENTS.md snippet, pre-commit hook).

Owner: workstream E (adapters).

Every adapter sits on this base (docs/PLAN.md, "Architecture"):

- the ``lawkeep`` CLI (every command has ``--json``);
- the MCP server (``lawkeep mcp``: tools ``check``, ``propose``, ``report``, ``list_laws``);
- an ``AGENTS.md`` section between ``<!-- lawkeep:begin -->`` / ``<!-- lawkeep:end -->`` markers;
- a git pre-commit hook that shows (does not block, unless asked) unrecorded law changes.

Agents with their own hooks and permission prompts get a richer adapter on top
(``claude_code``, ``codex``, ``cursor``, ``copilot``). All writes here are *merges*: existing
user settings are kept, and re-running never duplicates or overwrites what is there.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, cast

from lawkeep.errors import SetupError

BEGIN_MARK = "<!-- lawkeep:begin -->"
END_MARK = "<!-- lawkeep:end -->"
SH_BEGIN = "# lawkeep:begin"
SH_END = "# lawkeep:end"
MCP_SERVER_NAME = "lawkeep"
#: The order adapters are listed and installed in.
AGENT_ORDER = ("claude-code", "codex", "cursor", "copilot")


@dataclass
class InstallResult:
    agent: str
    files_changed: list[str] = field(default_factory=list[str])  # relative paths
    notes: list[str] = field(default_factory=list[str])  # plain English, shown when files changed
    warnings: list[str] = field(default_factory=list[str])  # plain English, always shown

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def changed(self, rel: str | None) -> None:
        if rel and rel not in self.files_changed:
            self.files_changed.append(rel)


class Adapter(Protocol):
    name: str  # matches config.AGENT_NAMES, e.g. "claude-code"

    def detect(self, root: Path) -> bool: ...

    def install(self, root: Path, *, hooks: bool = True) -> InstallResult: ...


def all_adapters() -> list[Adapter]:
    """One adapter per supported agent, in :data:`AGENT_ORDER`."""
    from lawkeep.adapters import claude_code, codex, copilot, cursor

    return [claude_code.ADAPTER, codex.ADAPTER, cursor.ADAPTER, copilot.ADAPTER]


def get_adapter(name: str) -> Adapter:
    for adapter in all_adapters():
        if adapter.name == name:
            return adapter
    raise SetupError(
        f"lawkeep has no adapter for the agent {name!r}.",
        hint="Choose one of: " + ", ".join(AGENT_ORDER) + ", all, other or none.",
    )


def detect_agents(root: Path) -> list[str]:
    """Names of the agents whose files are present in ``root`` (e.g. ``.claude/``, ``.cursor/``)."""
    return [a.name for a in all_adapters() if a.detect(root)]


# ---------------------------------------------------------------------------------------------
# Templates and small file helpers


def template(name: str) -> str:
    """Text of ``lawkeep/adapters/templates/<name>``."""
    return (resources.files("lawkeep.adapters") / "templates" / name).read_text(encoding="utf-8")


def rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def write_text(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_if_missing(root: Path, path: Path, text: str, result: InstallResult) -> bool:
    """Create ``path`` with ``text`` unless it exists. Returns True when written."""
    if path.exists():
        return False
    write_text(path, text)
    result.changed(rel(root, path))
    return True


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON settings file; missing/empty -> ``{}``; invalid -> SetupError (never clobber)."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SetupError(
            f"{path} is not valid JSON ({exc}), so lawkeep did not change it.",
            hint="Fix the file (or move it aside) and run `lawkeep init` again.",
        ) from None
    if not isinstance(data, dict):
        raise SetupError(
            f"{path} does not hold a JSON object, so lawkeep did not change it.",
            hint="Fix the file (or move it aside) and run `lawkeep init` again.",
        )
    return cast("dict[str, Any]", data)


def write_json(path: Path, data: dict[str, Any]) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def merge_json(
    root: Path, path: Path, update: Callable[[dict[str, Any]], object], result: InstallResult
) -> bool:
    """Load ``path``, let ``update(data)`` change it in place, write it if it changed."""
    data = read_json(path)
    before = json.dumps(data, sort_keys=True)
    update(data)
    if json.dumps(data, sort_keys=True) == before and path.exists():
        return False
    write_json(path, data)
    result.changed(rel(root, path))
    return True


def add_unique(items: list[Any], value: Any) -> bool:
    if value in items:
        return False
    items.append(value)
    return True


def ensure_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if isinstance(value, list):
        return cast("list[Any]", value)
    new: list[Any] = []
    data[key] = new
    return new


def ensure_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    new: dict[str, Any] = {}
    data[key] = new
    return new


def upsert_marked_block(
    root: Path, path: Path, body: str, result: InstallResult, *, header: str | None = None
) -> str:
    """Insert ``body`` between lawkeep markers in a Markdown file. Idempotent.

    Returns ``"created"`` (new file), ``"added"`` (appended to an existing file),
    ``"unchanged"`` (block already there and identical) or ``"kept"`` (a block is there but
    differs - lawkeep keeps the person's version and says how to refresh it).
    """
    block = f"{BEGIN_MARK}\n{body.strip()}\n{END_MARK}\n"
    if not path.exists():
        write_text(path, (header + "\n\n" if header else "") + block)
        result.changed(rel(root, path))
        return "created"
    text = path.read_text(encoding="utf-8")
    start, end = text.find(BEGIN_MARK), text.find(END_MARK)
    if start != -1 and end != -1 and end > start:
        current = text[start : end + len(END_MARK)] + "\n"
        if current.strip() == block.strip():
            return "unchanged"
        result.warnings.append(
            f"{rel(root, path)} already has a lawkeep section that differs from this version's; "
            "lawkeep kept yours. Delete the section (between the lawkeep markers) and run "
            "`lawkeep init` again to get the current text."
        )
        return "kept"
    sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    write_text(path, text + sep + block)
    result.changed(rel(root, path))
    return "added"


def launcher(args: str, root_expr: str) -> str:
    """A shell command that runs ``lawkeep <args>`` whether lawkeep is on PATH or in ``.venv``.

    If neither is found the command does nothing and succeeds, so a missing lawkeep never
    breaks the agent (law changes made meanwhile still show up as unrecorded in ``lawkeep check``).
    """
    local = f'"{root_expr}/.venv/bin/lawkeep"'
    return (
        f"if command -v lawkeep >/dev/null 2>&1; then lawkeep {args}; "
        f"elif [ -x {local} ]; then {local} {args}; fi"
    )


def mcp_launch_script(root_expr: str) -> str:
    """Shell code that starts ``lawkeep mcp`` from PATH, else from the project's ``.venv``."""
    local = f'"{root_expr}/.venv/bin/lawkeep"'
    return (
        f"if command -v lawkeep >/dev/null 2>&1; then exec lawkeep mcp; "
        f"elif [ -x {local} ]; then exec {local} mcp; "
        f"else echo 'lawkeep is not installed (not on PATH and not in .venv)' >&2; exit 1; fi"
    )


def mcp_server_entry(
    root_expr: str = "$(git rev-parse --show-toplevel 2>/dev/null || pwd)",
) -> dict[str, Any]:
    """The stdio MCP server entry used in ``.mcp.json`` / ``.cursor/mcp.json``.

    Like the hooks, it falls back to ``<project>/.venv/bin/lawkeep`` when lawkeep is not on
    PATH (a uv project with lawkeep as a dev dependency).
    """
    return {"command": "sh", "args": ["-c", mcp_launch_script(root_expr)]}


# ---------------------------------------------------------------------------------------------
# Shared base


def agents_snippet() -> str:
    return template("agents_snippet.md")


def install_agents_md(root: Path, result: InstallResult) -> str:
    """Add the lawkeep section to ``AGENTS.md`` (read by Codex, Cursor, Copilot and others)."""
    return upsert_marked_block(
        root,
        root / "AGENTS.md",
        agents_snippet(),
        result,
        header="# AGENTS.md\n\nInstructions for coding agents working in this project.",
    )


def git_dir(root: Path) -> Path | None:
    """The hooks folder git uses for ``root`` (honours ``core.hooksPath``), or None (no git)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    hooks = Path(out.stdout.strip())
    return hooks if hooks.is_absolute() else (root / hooks)


def install_pre_commit_hook(root: Path, result: InstallResult) -> str:
    """Install the git pre-commit hook. Returns created/added/unchanged/skipped/no_git."""
    hooks_dir = git_dir(root)
    if hooks_dir is None:
        result.warnings.append(
            "This folder is not a git repository yet, so no pre-commit hook was installed. "
            "Run `lawkeep init` again after `git init` to add it."
        )
        return "no_git"
    hook = hooks_dir / "pre-commit"
    block = template("pre-commit.sh")
    shown = rel(root, hook)
    if not hook.exists():
        write_text(hook, "#!/bin/sh\n" + block, executable=True)
        result.changed(shown)
        return "created"
    text = hook.read_text(encoding="utf-8", errors="replace")
    if SH_BEGIN in text:
        return "unchanged"
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!") and ("sh" in first) and "exit 0\n" not in text[-20:]:
        write_text(hook, text.rstrip("\n") + "\n\n" + block, executable=True)
        result.changed(shown)
        return "added"
    result.warnings.append(
        f"{shown} already exists and lawkeep could not safely add to it. To get the integrity "
        "check on every commit, add this line to it: lawkeep hook pre-commit"
    )
    return "skipped"


def ensure_gitignore(root: Path, entry: str, result: InstallResult) -> bool:
    path = root / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    wanted = {entry.rstrip("/"), entry, "/" + entry, "/" + entry.rstrip("/")}
    if any(line.strip() in wanted for line in text.splitlines()):
        return False
    sep = "" if (not text or text.endswith("\n")) else "\n"
    path.write_text(
        text + sep + "# lawkeep local state (not committed)\n" + entry + "\n", encoding="utf-8"
    )
    result.changed(".gitignore")
    return True


def is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)
