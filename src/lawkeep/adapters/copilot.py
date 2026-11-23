"""GitHub Copilot adapter (Copilot CLI, Copilot cloud/coding agent, VS Code agent mode).

Formats (checked 2026-11-21 against the official docs):

- Instructions: ``.github/copilot-instructions.md`` (repository custom instructions); Copilot's
  agents also read ``AGENTS.md``. https://docs.github.com/en/copilot/how-tos/configure-custom-instructions
- Hooks: ``.github/hooks/*.json`` ``{"version": 1, "hooks": {"preToolUse": [{"type": "command",
  "bash": "...", "powershell": "...", "cwd": "...", "timeoutSec": 30}]}}`` for Copilot CLI and
  the cloud agent (cloud agent: only ``bash`` entries, config must be on the default branch).
  ``preToolUse`` input: ``toolName`` (``bash``, ``edit``, ``create`` ...), ``toolArgs``, ``cwd``;
  output ``{"permissionDecision": "allow"|"deny"|"ask", "permissionDecisionReason": "..."}``
  ("ask" is treated as "deny" in the cloud agent and in CLI autopilot mode, where no person can
  answer). ``sessionStart`` may return ``{"additionalContext": "..."}``.
  https://docs.github.com/en/copilot/reference/hooks-reference
- MCP: the portable workspace file ``.mcp.json`` ``{"mcpServers": {...}}`` read by VS Code and
  Copilot CLI (``.vscode/mcp.json`` with ``servers`` is the older VS Code format). The cloud
  agent's MCP servers are set in the repository's settings on github.com, not in a file.
  https://code.visualstudio.com/docs/agents/reference/mcp-configuration

NOT verified in a running Copilot: the argument key holding the file path for ``edit`` /
``create`` (the docs type ``toolArgs`` as ``unknown``); lawkeep's hook looks for every common
key (``path``, ``file_path``, ``filePath`` ...). VS Code's own Copilot agent mode is not listed
as a hooks surface in GitHub's reference, so there it relies on the shared base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lawkeep.adapters import base
from lawkeep.models import as_dict

NAME = "copilot"
ROOT_EXPR = "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"


def hook_command(event: str) -> str:
    return base.launcher(f"hook {event} --agent {NAME}", ROOT_EXPR)


def hooks_update(data: dict[str, Any]) -> None:
    data.setdefault("version", 1)
    all_hooks = base.ensure_dict(data, "hooks")
    for event, name in (("preToolUse", "pre-tool-use"), ("sessionStart", "session-start")):
        entries = base.ensure_list(all_hooks, event)
        if any(f"lawkeep hook {name}" in str(as_dict(e).get("bash", "")) for e in entries):
            continue
        entries.append({"type": "command", "bash": hook_command(name), "timeoutSec": 30})


class CopilotAdapter:
    name = NAME

    def detect(self, root: Path) -> bool:
        gh = root / ".github"
        return (
            (gh / "copilot-instructions.md").is_file()
            or (gh / "hooks").is_dir()
            or (gh / "instructions").is_dir()
            or (root / ".vscode" / "mcp.json").is_file()
        )

    def install(self, root: Path, *, hooks: bool = True) -> base.InstallResult:
        result = base.InstallResult(agent=NAME)
        base.upsert_marked_block(
            root, root / ".github" / "copilot-instructions.md", base.agents_snippet(), result
        )
        if hooks:
            base.merge_json(root, root / ".github" / "hooks" / "lawkeep.json", hooks_update, result)

        def add_server(data: dict[str, Any]) -> None:
            base.ensure_dict(data, "mcpServers").setdefault(
                base.MCP_SERVER_NAME, base.mcp_server_entry()
            )

        base.merge_json(root, root / ".mcp.json", add_server, result)
        result.notes.append(
            "Copilot: the hooks in .github/hooks/lawkeep.json work in Copilot CLI and, once on "
            "your default branch, in the Copilot cloud agent. For the cloud agent, add the "
            "`lawkeep mcp` server in the repository's Copilot settings on github.com."
        )
        return result


ADAPTER = CopilotAdapter()
