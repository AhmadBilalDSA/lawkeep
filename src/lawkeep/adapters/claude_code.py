"""Claude Code adapter.

Formats (checked 2026-11-21 against the official docs):

- Hooks in ``.claude/settings.json``: ``{"hooks": {"PreToolUse": [{"matcher": "Edit|Write",
  "hooks": [{"type": "command", "command": "...", "timeout": 30}]}]}}``. A PreToolUse hook gets
  ``tool_name`` / ``tool_input`` / ``cwd`` as JSON on stdin and denies a call by printing
  ``{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": "..."}}`` (also ``"ask"``). ``$CLAUDE_PROJECT_DIR`` is the project
  root. SessionStart stdout is added to Claude's context.
  https://code.claude.com/docs/en/hooks
- Permission rules: ``{"permissions": {"ask": ["Bash(npm run *)"]}}``; deny > ask > allow, and a
  matching ask rule prompts even if a hook returned "allow". https://code.claude.com/docs/en/permissions
- Project MCP servers: ``.mcp.json`` ``{"mcpServers": {"name": {"command": ..., "args": [...]}}}``;
  Claude Code asks the person once before using a project server. https://code.claude.com/docs/en/mcp
- Claude Code reads ``CLAUDE.md`` (not ``AGENTS.md``); ``@AGENTS.md`` in it imports that file.
  https://code.claude.com/docs/en/memory

What this adapter installs (all merged into existing files, never overwriting):

1. a PreToolUse hook on ``Edit|Write|MultiEdit|NotebookEdit|Bash`` running
   ``lawkeep hook pre-tool-use --agent claude-code``: direct edits to ``laws/``, ``laws.lock`` or
   ``lawkeep.toml`` are denied with a message saying to use ``lawkeep propose``; ``lawkeep
   approve`` gets "ask" so the person confirms it in Claude Code's own permission prompt;
2. a SessionStart hook (``lawkeep hook session-start``) that marks the session start for the
   end-of-session report and reminds Claude of the lawkeep rules;
3. ``permissions.ask`` rules for ``lawkeep approve`` (also through ``uv run`` / ``python -m``),
   so the person's yes comes from Claude Code's prompt even without the hook;
4. the ``lawkeep`` MCP server in ``.mcp.json``;
5. ``@AGENTS.md`` in ``CLAUDE.md`` so Claude reads the shared lawkeep instructions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from lawkeep.adapters import base
from lawkeep.models import as_dict, as_list

NAME = "claude-code"
ROOT_EXPR = "$CLAUDE_PROJECT_DIR"
EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
SESSION_MATCHER = "startup|resume|clear|compact"
HOOK_MARK = "lawkeep hook"

#: Ask rules: the person confirms these in Claude Code's own permission prompt.
ASK_RULES = (
    "Bash(lawkeep approve *)",
    "Bash(uv run lawkeep approve *)",
    "Bash(uvx lawkeep approve *)",
    "Bash(python -m lawkeep approve *)",
    "Bash(python3 -m lawkeep approve *)",
    "Bash(.venv/bin/lawkeep approve *)",
)


def hook_command(event: str) -> str:
    return base.launcher(f"hook {event} --agent {NAME}", ROOT_EXPR)


def _has_lawkeep_hook(groups: list[Any], event: str) -> bool:
    for group in groups:
        for h in as_list(as_dict(group).get("hooks")):
            if f"{HOOK_MARK} {event}" in str(as_dict(h).get("command", "")):
                return True
    return False


def settings_update(*, hooks: bool = True) -> Callable[[dict[str, Any]], None]:
    """Return a function that merges lawkeep's entries into a settings dict (in place)."""

    def update(data: dict[str, Any]) -> None:
        if hooks:
            all_hooks = base.ensure_dict(data, "hooks")
            for event, matcher, name in (
                ("PreToolUse", EDIT_MATCHER, "pre-tool-use"),
                ("SessionStart", SESSION_MATCHER, "session-start"),
            ):
                groups = base.ensure_list(all_hooks, event)
                if not _has_lawkeep_hook(groups, name):
                    groups.append(
                        {
                            "matcher": matcher,
                            "hooks": [
                                {"type": "command", "command": hook_command(name), "timeout": 30}
                            ],
                        }
                    )
        perms = base.ensure_dict(data, "permissions")
        ask = base.ensure_list(perms, "ask")
        for rule in ASK_RULES:
            base.add_unique(ask, rule)

    return update


class ClaudeCodeAdapter:
    name = NAME

    def detect(self, root: Path) -> bool:
        return (root / ".claude").is_dir() or (root / "CLAUDE.md").is_file()

    def install(self, root: Path, *, hooks: bool = True) -> base.InstallResult:
        result = base.InstallResult(agent=NAME)
        base.merge_json(
            root, root / ".claude" / "settings.json", settings_update(hooks=hooks), result
        )

        def add_server(data: dict[str, Any]) -> None:
            servers = base.ensure_dict(data, "mcpServers")
            servers.setdefault(
                base.MCP_SERVER_NAME, base.mcp_server_entry("${CLAUDE_PROJECT_DIR:-.}")
            )

        base.merge_json(root, root / ".mcp.json", add_server, result)
        self._claude_md(root, result)
        if hooks:
            result.notes.append(
                "Claude Code: direct edits to laws/ are turned into a request to use "
                "`lawkeep propose`, and `lawkeep approve` asks you to confirm in Claude Code's "
                "own permission prompt."
            )
        result.notes.append(
            "Claude Code asks you once to allow the project's `lawkeep` MCP server "
            "(from .mcp.json) the next time it starts here."
        )
        return result

    @staticmethod
    def _claude_md(root: Path, result: base.InstallResult) -> None:
        path = root / "CLAUDE.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if any(line.strip() == "@AGENTS.md" for line in text.splitlines()):
                return
        base.upsert_marked_block(
            root,
            path,
            "Read and follow AGENTS.md (it includes the lawkeep rules for this project):\n\n@AGENTS.md",
            result,
        )


ADAPTER = ClaudeCodeAdapter()
