"""Is a person there to answer? A best guess for coding agents (``person_present``).

lawkeep cannot know whether someone is watching a coding agent. This module makes a plain,
documented guess so an agent can choose between "stop and ask the person" (someone is there)
and "park and keep going" (a long run with nobody there: file a proposal, carry on with the
rest of the task, list what waits in the final summary). The guess never changes what lawkeep
allows: a weakening change always waits for a person's yes either way.

The rules, first match wins:

1. ``LAWKEEP_PERSON_PRESENT`` set to ``1``/``yes``/``true`` or ``0``/``no``/``false``: that
   answer. Set it in the script that starts an overnight run (``LAWKEEP_PERSON_PRESENT=0``).
2. ``CI`` set (GitHub Actions and most CI services set it): nobody is there.
3. Standard input is a terminal: a person typed the command, so they are there.
4. Claude Code: ``CLAUDE_CODE_SESSION_ATTENDED`` (``1`` there, ``0`` not); else
   ``CLAUDE_CODE_ENTRYPOINT`` ``cli`` (the interactive terminal app) means there,
   ``sdk-*`` (``claude -p`` and the Agent SDK) means not. These variables were seen in Claude
   Code 2.1 on 2026-11-23; they are not a documented contract and may change.
5. Anything else (Codex, Cursor, Copilot, scripts): nobody is known to be there. Those agents
   set no variable lawkeep knows of, so an interactive session of theirs reads as "not there";
   the agent instructions say that a person chatting in the session counts as present.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

ENV = "LAWKEEP_PERSON_PRESENT"
_YES = {"1", "yes", "true", "on"}
_NO = {"0", "no", "false", "off"}


@dataclass(frozen=True)
class Presence:
    present: bool
    reason: str  # one plain sentence: why lawkeep thinks so

    def to_dict(self) -> dict[str, object]:
        return {"person_present": self.present, "person_present_reason": self.reason}


def _stdin_is_terminal() -> bool:
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (OSError, ValueError):
        return False


def detect(env: Mapping[str, str] | None = None, *, stdin_tty: bool | None = None) -> Presence:
    """lawkeep's guess whether a person is there to answer (see the module docstring)."""
    env = os.environ if env is None else env
    forced = env.get(ENV, "").strip().lower()
    if forced in _YES:
        return Presence(True, f"{ENV} says a person is there.")
    if forced in _NO:
        return Presence(False, f"{ENV} says nobody is there.")
    if env.get("CI", "").strip().lower() not in ("", "0", "false"):
        return Presence(False, "This runs in CI, where nobody can answer.")
    tty = _stdin_is_terminal() if stdin_tty is None else stdin_tty
    if tty:
        return Presence(True, "The command was typed in a terminal.")
    attended = env.get("CLAUDE_CODE_SESSION_ATTENDED", "").strip()
    if attended in ("1", "0"):
        if attended == "1":
            return Presence(True, "Claude Code says a person is in this session.")
        return Presence(False, "Claude Code says nobody is in this session.")
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_ENTRYPOINT"):
        entry = env.get("CLAUDE_CODE_ENTRYPOINT", "")
        if entry == "cli":
            return Presence(True, "This is an interactive Claude Code session.")
        if entry.startswith("sdk"):
            return Presence(False, "This is a non-interactive Claude Code run (claude -p or SDK).")
    return Presence(
        False,
        "No terminal and no known interactive agent session, so lawkeep assumes nobody is "
        "there. If a person is chatting with you in this session, they are there.",
    )
