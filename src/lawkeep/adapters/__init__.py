"""Thin per-agent adapters on one shared base.

Owner: workstream E (adapters).

- ``base``: the shared base every agent gets (AGENTS.md section, git pre-commit hook, MCP
  server config helpers), the ``Adapter`` protocol and the registry (``all_adapters``).
- ``guard``: decides whether an agent's tool call would change the laws directly (used by
  ``lawkeep hook pre-tool-use`` for every agent).
- ``claude_code``, ``codex``, ``cursor``, ``copilot``: one thin adapter per agent, each using
  the agent's own hooks, permission prompts and MCP config where it has them.
"""
