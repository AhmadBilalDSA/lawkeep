"""Cursor adapter (editor agent and Cursor CLI).

Formats (checked 2026-11-21 against the official docs and the Cursor forum):

- Instructions: project rules in ``.cursor/rules/*.mdc`` with front matter
  (``description``, ``globs``, ``alwaysApply``); Cursor also reads ``AGENTS.md``.
  https://cursor.com/docs/context/rules
- Hooks: ``.cursor/hooks.json`` ``{"version": 1, "hooks": {"<event>": [{"command": "...",
  "matcher": "..."}]}}``; project hooks run from the project root.
  ``preToolUse`` (tools such as ``Shell``, ``Read``, ``Write``, ``Delete``) answers
  ``{"permission": "allow"|"deny", "user_message": ..., "agent_message": ...}`` ("ask" is accepted
  but not enforced there today); ``beforeShellExecution`` (input ``command``, ``cwd``) supports
  ``"ask"``; ``sessionStart`` may return ``additional_context`` but is fire-and-forget.
  Exit 2 also blocks; other failures fail open.  https://cursor.com/docs/hooks
- MCP: ``.cursor/mcp.json`` ``{"mcpServers": {...}}``. https://cursor.com/docs/context/mcp

NOT verified in a running Cursor: the exact ``tool_input`` key that holds the file path for
``Write`` in ``preToolUse`` (the docs only show a Shell example). lawkeep's hook therefore
looks for every common key (``file_path``, ``path``, ``target_file`` ...). Forum reports also
say hooks sometimes did not fire; anything missed still shows up as an unrecorded law change.
Cursor has no per-command "ask" permission rules in project settings, so the person's yes
for ``lawkeep approve`` comes from the ``beforeShellExecution`` hook answering "ask".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lawkeep.adapters import base
from lawkeep.models import as_dict

NAME = "cursor"
ROOT_EXPR = "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
EDIT_MATCHER = "Write|Edit|StrReplace|MultiEdit|Delete|ApplyPatch"


def hook_command(event: str) -> str:
    return base.launcher(f"hook {event} --agent {NAME}", ROOT_EXPR)


def rule_text() -> str:
    return (
        "---\n"
        "description: lawkeep - laws this code must obey; how to check and change them\n"
        "alwaysApply: true\n"
        "---\n\n" + base.agents_snippet()
    )


def hooks_update(data: dict[str, Any]) -> None:
    data.setdefault("version", 1)
    all_hooks = base.ensure_dict(data, "hooks")
    wanted = (
        ("preToolUse", "pre-tool-use", EDIT_MATCHER),
        ("beforeShellExecution", "pre-tool-use", None),
        ("sessionStart", "session-start", None),
    )
    for event, name, matcher in wanted:
        entries = base.ensure_list(all_hooks, event)
        if any(f"lawkeep hook {name}" in str(as_dict(e).get("command", "")) for e in entries):
            continue
        entry: dict[str, Any] = {"command": hook_command(name)}
        if matcher:
            entry["matcher"] = matcher
        entries.append(entry)


class CursorAdapter:
    name = NAME

    def detect(self, root: Path) -> bool:
        return (root / ".cursor").is_dir() or (root / ".cursorrules").is_file()

    def install(self, root: Path, *, hooks: bool = True) -> base.InstallResult:
        result = base.InstallResult(agent=NAME)
        base.write_if_missing(root, root / ".cursor" / "rules" / "lawkeep.mdc", rule_text(), result)
        if hooks:
            base.merge_json(root, root / ".cursor" / "hooks.json", hooks_update, result)

        def add_server(data: dict[str, Any]) -> None:
            base.ensure_dict(data, "mcpServers").setdefault(
                base.MCP_SERVER_NAME, base.mcp_server_entry()
            )

        base.merge_json(root, root / ".cursor" / "mcp.json", add_server, result)
        result.notes.append(
            "Cursor: enable the `lawkeep` MCP server in Cursor's MCP settings if it is not on. "
            "Cursor hooks are new; if a direct law edit slips through, `lawkeep check` still "
            "flags it as an unrecorded law change."
        )
        return result


ADAPTER = CursorAdapter()
