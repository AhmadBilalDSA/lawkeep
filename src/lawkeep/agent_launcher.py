"""Start the user's own coding agent in a separate, non-interactive session (for ``draft``).

Owner: workstream D (law lifecycle).

The command comes from ``lawkeep.toml``::

    [agent]
    name = "claude-code"          # picks a preset below
    command = "..."               # or your own command; {prompt} / {prompt_file} placeholders,
                                  # with neither the prompt is sent on stdin
    timeout_seconds = 900

Presets (checked against each agent's public docs on 2026-11-21; a real agent was NOT run):

- Claude Code: ``claude -p --permission-mode acceptEdits {prompt}``
  ``-p`` is print (non-interactive) mode; ``acceptEdits`` lets it write files without asking,
  but not run shell commands. Source: https://docs.claude.com/en/docs/claude-code/headless
- Codex: ``codex exec --sandbox workspace-write {prompt}``
  ``codex exec`` is non-interactive and read-only by default; ``--sandbox workspace-write``
  allows edits in the project. ``--full-auto`` is kept by
  Codex only as a deprecated alias that prints a warning.
  Sources: https://developers.openai.com/codex/noninteractive ,
  https://developers.openai.com/codex/cli/reference
- Cursor: ``cursor-agent -p --force {prompt}``
  ``-p``/``--print`` is non-interactive; ``--force`` lets print mode change files (without it
  changes are only proposed). Newer docs call the binary ``agent``; ``cursor-agent`` still
  appears in the docs. Team admins can switch print mode off. Source:
  https://cursor.com/docs/cli/headless
- GitHub Copilot CLI (changed 2026-11-23, docs checked that day; not run):
  ``copilot -p {prompt} --no-ask-user --allow-tool 'read, write(laws/drafts/*),
  write(laws/strategies.py)' --deny-tool 'shell, url'``.
  ``-p`` runs one prompt non-interactively; ``--no-ask-user`` stops it waiting for answers.
  ``--allow-tool``/``--deny-tool`` take ``Kind(argument)`` patterns (kinds ``read``,
  ``write``, ``shell``, ``url``, MCP servers; ``write(src/*.ts)`` is the reference's own
  example of a path pattern), and deny rules always win over allow rules. So Copilot may read
  the project and write draft files and ``laws/strategies.py``, but can run no shell command
  and fetch no URL. The old preset ``--allow-all-tools`` let it run any shell command.
  Not confirmed by the docs: whether a tool that is neither allowed nor denied is refused in
  ``-p`` mode or makes the run fail, and whether the write pattern matches nested folders. If
  Copilot cannot write a draft, the draft run fails safely (no draft); set ``[agent] command``
  in ``lawkeep.toml`` to widen it. Sources:
  https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference
  (options ``--allow-tool``, ``--deny-tool``, ``--no-ask-user``, "Tool permission patterns"),
  https://docs.github.com/en/copilot/how-tos/copilot-cli/use-copilot-cli/allowing-tools
  ("Deny rules always take precedence over allow rules"),
  https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically
  ("Always give minimal permissions").

Tests inject ``runner``; they never start a real agent.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lawkeep.config import AGENT_PRESETS, AgentConfig
from lawkeep.errors import ConfigError, SetupError

#: Launch commands lawkeep uses when ``[agent] command`` is empty (see module docstring).
#: One source of truth: ``config.AGENT_PRESETS``.
VERIFIED_PRESETS: dict[str, str] = AGENT_PRESETS

#: Linux refuses a single argument longer than 128 KiB; stay well below.
MAX_ARG_PROMPT = 100_000

#: Environment variable set for the agent, so hooks can tell a drafting session apart.
DRAFTING_ENV = "LAWKEEP_DRAFTING"


@dataclass
class LaunchResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def effective_agent(agent: AgentConfig) -> AgentConfig:
    """The agent config with lawkeep's checked preset filled in when no command is set."""
    if agent.command:
        return agent
    preset = VERIFIED_PRESETS.get(agent.name)
    if preset:
        return AgentConfig(name=agent.name, command=preset, timeout_seconds=agent.timeout_seconds)
    return agent


def build_argv(
    agent: AgentConfig, prompt: str, prompt_file: str | os.PathLike[str] | None = None
) -> tuple[list[str], str | None]:
    """``(argv, stdin_text)`` for this agent. Raises ConfigError when no agent is set up."""
    eff = effective_agent(agent)
    argv, stdin_text = eff.argv(prompt, prompt_file)
    if stdin_text is None and any(prompt in a for a in argv) and len(prompt) > MAX_ARG_PROMPT:
        raise ConfigError(
            f"The drafting prompt is {len(prompt):,} characters, too long to pass as one "
            f"command-line argument.",
            hint="Use {prompt_file} (or no placeholder, to send it on stdin) in [agent] command, "
            "or pass fewer/shorter --docs files.",
        )
    return argv, stdin_text


def _default_runner(
    argv: list[str],
    *,
    cwd: str,
    input: str | None,
    timeout: float | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=None if input is not None else subprocess.DEVNULL,
    )


def launch(
    agent: AgentConfig,
    prompt: str,
    *,
    cwd: Path,
    timeout: float | None = None,
    runner: Callable[..., Any] | None = None,
) -> LaunchResult:
    """Run the agent once with ``prompt`` and wait for it to finish.

    ``runner(argv, *, cwd, input, timeout, env)`` must return an object with ``returncode``,
    ``stdout`` and ``stderr`` (like ``subprocess.CompletedProcess``); it defaults to
    ``subprocess.run``. Raises SetupError when the agent cannot be started or times out.
    """
    cwd = Path(cwd)
    timeout = timeout if timeout is not None else (agent.timeout_seconds or None)
    run = runner or _default_runner
    env = dict(os.environ)
    env[DRAFTING_ENV] = "1"
    with tempfile.TemporaryDirectory(prefix="lawkeep-draft-") as tmp:
        prompt_file = Path(tmp) / "lawkeep-draft-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv, stdin_text = build_argv(agent, prompt, prompt_file)
        start = time.monotonic()
        try:
            proc = run(argv, cwd=str(cwd), input=stdin_text, timeout=timeout, env=env)
        except FileNotFoundError:
            raise SetupError(
                f"Could not start your coding agent: the command `{argv[0]}` was not found.",
                hint="Install the agent's command-line tool, or set [agent] command in "
                "lawkeep.toml to the command that starts it.",
            ) from None
        except subprocess.TimeoutExpired:
            raise SetupError(
                f"Your coding agent did not finish within {int(timeout or 0)} seconds and "
                f"was stopped.",
                hint="Raise [agent] timeout_seconds in lawkeep.toml, or draft fewer laws at once.",
            ) from None
        except PermissionError as exc:
            raise SetupError(f"Could not start your coding agent: {exc}") from None
        duration = time.monotonic() - start
    return LaunchResult(
        argv=[a if a != prompt else "<prompt>" for a in argv],
        returncode=int(getattr(proc, "returncode", 0) or 0),
        stdout=str(getattr(proc, "stdout", "") or ""),
        stderr=str(getattr(proc, "stderr", "") or ""),
        duration_seconds=round(duration, 3),
    )
