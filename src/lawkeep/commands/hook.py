"""`lawkeep hook`: entry point for agent hooks and the git pre-commit hook.

Owner: workstream E (adapters).

Usage::

    lawkeep hook pre-tool-use --agent claude-code   # agent hook payload on stdin
    lawkeep hook claude-code                        # same (agent first, event defaults to pre-tool-use)
    lawkeep hook session-start --agent codex
    lawkeep hook session-end
    lawkeep hook pre-commit                         # the git pre-commit hook

Events:

- ``pre-tool-use`` (alias ``pre-edit``): reads the agent's hook JSON on stdin. A direct edit of
  ``laws/``, ``laws.lock`` or ``lawkeep.toml`` (edit tools, Codex patches, and shell commands
  judged by what they write to, not by what they mention; see ``adapters/guard.py`` and
  ``adapters/writes.py``) is denied with a message telling the agent to use ``lawkeep propose``;
  ``lawkeep approve`` gets "ask" where the agent supports it, so the person's yes comes from the
  agent's own permission prompt. Output is in the agent's own hook format (see
  :func:`agent_output`); ``--json`` prints lawkeep's report instead (for debugging). Always
  result code 0: the decision is in the output, and a broken hook never blocks the agent.
- ``session-start``: marks the session start (``.lawkeep/session.json``) for the
  end-of-session report and prints a short reminder that agents add to their context: the
  rules, what to do when nobody is there (``NO_PERSON``), lawkeep's guess whether a person is
  there (``presence.detect``) and, when something waits, the one-line count
  ("N things wait for your decision — run `lawkeep approve`", ``waiting.collect``).
  A resumed or compacted session keeps its original start time.
- ``session-end``: prints the law change report for this session, ending with the same
  waiting count. Result code 0.
- ``pre-commit``: runs the integrity part of ``lawkeep check`` (no project code runs) and prints
  what it flags. It does not block the commit (result code 0) unless the person asked for that
  with ``git config lawkeep.precommit block`` (or ``LAWKEEP_PRECOMMIT=block``): then flags that
  need a person's decision give result code 1, which stops the commit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, TextIO, cast

from rich.console import RenderableType

from lawkeep import exitcodes, history, paths
from lawkeep.adapters import guard
from lawkeep.errors import LawkeepError
from lawkeep.models import IntegrityIssue, SimpleReport
from lawkeep.output import emit

HELP = "used by coding agents and git to call lawkeep (you do not need to run it)"

EVENTS = ("pre-tool-use", "pre-edit", "session-start", "session-end", "pre-commit")
AGENTS = ("claude-code", "codex", "cursor", "copilot", "other", "git")

REMINDER = (
    "This project uses lawkeep: laws (rules the code must always follow) live in laws/. "
    "Before you finish, run `lawkeep check`; it must say ALL LAWS HOLD. If a law fails, fix the "
    "code, not the law. Never edit laws/, laws.lock or lawkeep.toml directly; to change a law, "
    "use `lawkeep propose`. If a law conflicts with the task, stop and ask the person (that is "
    "a successful outcome). At the end of the session, show the person `lawkeep report`."
)

#: What to do when nobody is there to ask (long runs on your own). Same rules as the
#: AGENTS.md snippet ("When no person is there").
NO_PERSON = (
    "If nobody is there to ask (a long run on your own; `lawkeep check --json` shows "
    "`person_present: false`) and a law blocks you: file the change with `lawkeep propose "
    '<LAW_ID> --file laws/drafts/<file>.py --reason "<why>"` (looser or unclear changes wait '
    "for the person), never work around or edit the law, never run `lawkeep approve`, keep "
    "going with every part of the task that does not depend on that law, and end with a "
    "summary: done / waiting for the person (with the ids) / not done because blocked."
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        metavar="EVENT",
        help="pre-tool-use, session-start, session-end or pre-commit "
        "(or an agent name, then the event defaults to pre-tool-use)",
    )
    parser.add_argument("event", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--agent",
        choices=AGENTS,
        default=None,
        help="which agent calls the hook (decides the output format; default claude-code style)",
    )


def _resolve_event(args: argparse.Namespace) -> tuple[str, str]:
    target, event, agent = args.target, args.event, args.agent
    if not target:
        raise LawkeepError(
            "`lawkeep hook` needs an event.",
            hint="Events: pre-tool-use, session-start, session-end, pre-commit. Example: "
            "`lawkeep hook pre-tool-use --agent claude-code` (agent hook JSON on stdin).",
        )
    if target in AGENTS:
        agent = agent or target
        target = event or "pre-tool-use"
    elif event:
        raise LawkeepError(
            f"`lawkeep hook {target} {event}` is not understood.",
            hint="Use `lawkeep hook <event> --agent <name>`, e.g. "
            "`lawkeep hook pre-tool-use --agent claude-code`.",
        )
    if target == "pre-edit":
        target = "pre-tool-use"
    if target not in EVENTS:
        raise LawkeepError(
            f"Unknown hook event {target!r}.",
            hint="Events: pre-tool-use, session-start, session-end, pre-commit.",
        )
    return target, (agent or "claude-code")


def read_payload(stream: TextIO | None = None) -> dict[str, Any]:
    """The hook JSON from stdin ({} when there is none, it is a terminal, or it is not JSON)."""
    stream = stream or sys.stdin
    if stream is None:  # no stdin at all
        return {}
    try:
        if stream.isatty():
            return {}
        text = stream.read()
    except (OSError, ValueError):
        return {}
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return cast("dict[str, Any]", data) if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------------------------
# Output in each agent's own hook format


def agent_output(agent: str, decision: guard.Decision) -> dict[str, Any] | None:
    """The JSON to print for a PreToolUse decision, or None to print nothing (no decision)."""
    action, msg = decision.action, decision.message
    if agent == "cursor":
        # Cursor permission hooks need a valid answer on exit 0, even for "allow".
        out: dict[str, Any] = {"permission": action}
        if action != "allow":
            out["user_message"] = msg
            out["agent_message"] = msg
        return out
    if action == "allow":
        return None
    if agent == "copilot":
        return {"permissionDecision": action, "permissionDecisionReason": msg}
    if agent == "codex" and action == "ask":
        return None  # Codex PreToolUse cannot ask; .codex/rules/lawkeep.rules prompts instead
    # Claude Code, Codex (deny) and other agents that accept the Claude Code format.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": action,
            "permissionDecisionReason": msg,
        }
    }


def session_output(agent: str, text: str) -> str:
    if agent == "cursor":
        return json.dumps({"additional_context": text})
    if agent == "copilot":
        return json.dumps({"additionalContext": text})
    return text  # Claude Code and Codex add plain stdout to the agent's context


# ---------------------------------------------------------------------------------------------
# Events


def _payload_root(args: argparse.Namespace, payload: dict[str, Any]) -> Path | None:
    if args.root:
        return paths.find_root(args.root)
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR")
    if cwd:
        return paths.find_root(cwd)
    return paths.find_root()


def pre_tool_use(args: argparse.Namespace, agent: str) -> int:
    payload = read_payload()
    decision = guard.decide(payload)
    if args.json:
        emit(
            SimpleReport(
                kind="hook",
                exit_code=exitcodes.ALL_HOLD,
                summary=f"pre-tool-use: {decision.action}",
                lines=[decision.message] if decision.message else [],
                data={"event": "pre-tool-use", "agent": agent, **decision.to_dict()},
            ),
            True,
        )
        return exitcodes.ALL_HOLD
    out = agent_output(agent, decision)
    if out is not None:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return exitcodes.ALL_HOLD


def _waiting_note(root: Path) -> str | None:
    """The one-line count of everything waiting for the person (``waiting.collect``)."""
    try:
        from lawkeep import waiting

        line = waiting.collect(root).line()
    except Exception:  # noqa: BLE001 - a hook must never break the agent
        return None
    if not line:
        return None
    return f"For the person: {line} Show them `lawkeep report`; they decide."


def _session_data(root: Path) -> dict[str, Any]:
    """Additive JSON for session-start: the waiting list and the person-present guess."""
    from lawkeep import presence, waiting

    out: dict[str, Any] = dict(presence.detect().to_dict())
    try:
        out["waiting_for_person"] = waiting.collect(root).to_dict()
    except Exception:  # noqa: BLE001
        pass
    return out


def _presence_note() -> str:
    from lawkeep import presence

    seen = presence.detect()
    word = "is probably there" if seen.present else "is probably not there"
    return f"lawkeep's guess: a person {word} ({seen.reason})"


def session_start(args: argparse.Namespace, agent: str) -> int:
    payload = read_payload()
    root = _payload_root(args, payload)
    if root is None:
        if args.json:
            emit(SimpleReport(kind="hook", summary="Not a lawkeep project; nothing to do."), True)
        return exitcodes.ALL_HOLD
    source = str(payload.get("source") or "")
    existing = history.session_start(root)
    if source in ("resume", "compact") and existing:
        started = existing
    else:
        started = history.mark_session_start(root, agent=agent if agent != "other" else None)
    text = f"{REMINDER} {NO_PERSON} {_presence_note()}"
    note = _waiting_note(root)
    if note:
        text += " " + note
    if args.json:
        emit(
            SimpleReport(
                kind="hook",
                summary=f"Session started at {started}.",
                lines=[text],
                data={
                    "event": "session-start",
                    "agent": agent,
                    "started_at": started,
                    **_session_data(root),
                },
            ),
            True,
        )
    else:
        sys.stdout.write(session_output(agent, text) + "\n")
    return exitcodes.ALL_HOLD


def session_end(args: argparse.Namespace, agent: str) -> int:
    payload = read_payload()
    root = _payload_root(args, payload)
    if root is None:
        return exitcodes.ALL_HOLD
    since = history.session_start(root)
    from lawkeep import lawchanges, waiting

    text = lawchanges.render_since(root, since, waiting_list=waiting.collect(root))
    if args.json:
        emit(
            SimpleReport(
                kind="hook",
                summary="Law changes in this session.",
                lines=text.splitlines(),
                data={"event": "session-end", "agent": agent, "since": since},
            ),
            True,
        )
    else:
        sys.stdout.write(text.rstrip("\n") + "\n")
    return exitcodes.ALL_HOLD


def _blocking_requested(root: Path) -> bool:
    if os.environ.get("LAWKEEP_PRECOMMIT", "").strip().lower() == "block":
        return True
    try:
        out = subprocess.run(
            ["git", "config", "--get", "lawkeep.precommit"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip().lower() == "block"


def pre_commit(args: argparse.Namespace, agent: str) -> int:
    root = paths.find_root(args.root) if args.root else paths.find_root()
    if root is None:
        return exitcodes.ALL_HOLD
    notes: list[str] = []
    from lawkeep import config, integrity

    try:
        cfg = config.load_config(root)
        notes.extend(cfg.warnings)
    except LawkeepError as exc:  # a broken lawkeep.toml must not hide the other flags
        notes.append(exc.message)
        cfg = config.default_config()
    issues = integrity.check_integrity(root, cfg)
    errors = [i for i in issues if i.severity == "error"]
    block = bool(errors) and _blocking_requested(root)
    code = exitcodes.SOME_FAIL if block else exitcodes.ALL_HOLD
    lines = [f"  - {i.message}" for i in issues]
    if issues:
        summary = (
            f"lawkeep: {len(issues)} thing(s) to look at before you commit"
            + (" (commit stopped: git config lawkeep.precommit is 'block')" if block else "")
            + ":"
        )
    else:
        summary = "lawkeep: no law changes outside lawkeep."
    report = SimpleReport(
        kind="hook",
        exit_code=code,
        summary=summary,
        lines=lines,
        hint=("Run `lawkeep report` for details." if issues else None),
        data={
            "event": "pre-commit",
            "blocking": block,
            "issues": [i.to_dict() for i in issues],
            "notes": notes,
        },
    )
    if args.json:
        emit(report, True)
    elif issues or notes:
        sys.stderr.write(_pre_commit_text(issues, notes, block))
        sys.stderr.flush()
    return code


def _pre_commit_text(issues: list[IntegrityIssue], notes: list[str], block: bool) -> str:
    """What a person sees in `git commit` (stderr; plain unless it is a terminal)."""
    from lawkeep import ui

    errors = [i for i in issues if i.severity == "error"]
    out: list[RenderableType] = []
    if block:
        out.append(
            ui.status(
                "fail",
                "lawkeep stopped this commit (you asked for that with `git config "
                "lawkeep.precommit block`):",
            )
        )
    elif errors:
        out.append(
            ui.status(
                "warn",
                f"lawkeep: {len(issues)} thing(s) to look at before you commit. The commit goes "
                "ahead; lawkeep only tells you.",
            )
        )
    elif issues:
        out.append(ui.status("info", f"lawkeep: {len(issues)} note(s) about this commit:"))
    for i in issues:
        out.append(ui.indent(ui.status("warn" if i.severity == "error" else "info", i.message)))
    for n in notes:
        out.append(ui.indent(ui.status("info", n)))
    if issues:
        out.append(ui.next_step("run `lawkeep report` to see what changed and what to do."))
    return ui.render(*out, stream=sys.stderr)


def run(args: argparse.Namespace) -> int:
    event, agent = _resolve_event(args)
    handler = {
        "pre-tool-use": pre_tool_use,
        "session-start": session_start,
        "session-end": session_end,
        "pre-commit": pre_commit,
    }[event]
    if event == "pre-commit":
        return handler(args, agent)
    try:
        return handler(args, agent)
    except Exception as exc:  # an agent hook must never break the agent
        if os.environ.get("LAWKEEP_DEBUG"):
            raise
        sys.stderr.write(f"lawkeep hook {event}: {exc}\n")
        if event == "pre-tool-use" and agent == "cursor" and not args.json:
            sys.stdout.write(json.dumps({"permission": "allow"}) + "\n")  # Cursor needs an answer
        return exitcodes.ALL_HOLD
