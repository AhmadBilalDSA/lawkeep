"""MCP server exposing lawkeep to coding agents: tools ``check``, ``propose``, ``report``, ``list_laws``.

Owner: workstream E (adapters). Needs the optional ``mcp`` package (``pip install
'lawkeep[mcp]'``), imported lazily so the core works without it. Works with the ``mcp`` SDK 2.x
(``mcp.server.mcpserver.MCPServer``) and 1.x (``mcp.server.fastmcp.FastMCP``).

Each tool returns exactly the JSON document the matching CLI command prints with ``--json``
(``check``, ``propose``, ``report``): the server runs ``python -m lawkeep <command> --json`` in a
fresh process, so the MCP path and the CLI path can never disagree. ``list_laws`` has no CLI
command; it returns a ``SimpleReport`` (kind ``list_laws``) built from static discovery (no
project code runs).

There is deliberately no ``approve`` tool: approving a law change is a person's decision, made
through ``lawkeep approve`` in the terminal or the agent's own permission prompt.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from lawkeep import exitcodes, paths
from lawkeep.errors import SetupError
from lawkeep.models import SimpleReport
from lawkeep.output import to_jsonable

INSTALL_HINT = "Install it with: pip install 'lawkeep[mcp]'   (or, with uv: uv add 'lawkeep[mcp]')"

INSTRUCTIONS = (
    "lawkeep keeps laws: rules this project's code must obey, each a plain-English sentence plus "
    "a check. Call `check` before finishing a task; on SOME LAWS FAIL, read the simplest failing "
    "example and fix the code, not the law. Never edit laws/, laws.lock or lawkeep.toml directly: "
    "to change a law call `propose` (weakening waits for the person's yes). If a law conflicts "
    "with the task and the person is there, stop and ask them. If nobody is there (a long run "
    "on your own; `check` returns `person_present: false`): call `propose` with your reason, "
    "never work around or edit the law, never approve anything, keep going with every part of "
    "the task that does not depend on that law, and end with a summary: done / waiting for the "
    "person (with the ids) / not done because blocked. Show the person `report` at the end of "
    "the session. Result codes: 0 all laws hold, 1 some fail, 2 waiting for a person's "
    "decision, 3 setup problem. `waiting_for_person` in the results counts everything that "
    "waits for the person."
)

#: Runs a command and returns (returncode, stdout, stderr). Injectable for tests.
Runner = Callable[[list[str], Path | None, float | None], tuple[int, str, str]]


def _default_runner(argv: list[str], cwd: Path | None, timeout: float | None):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


class LawkeepTools:
    """The tool implementations, independent of the MCP SDK (tested directly)."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        runner: Runner | None = None,
        actor: str = "agent:other",
        timeout: float | None = None,
    ) -> None:
        self.root = Path(root).resolve() if root else None
        self.runner = runner or _default_runner
        self.actor = actor
        self.timeout = timeout

    # -- helpers --------------------------------------------------------------------------------

    def _project(self) -> Path | None:
        return self.root or paths.find_root()

    def run_cli(self, command: str, *args: str) -> str:
        """Run ``python -m lawkeep <command> <args> --json [--root R]``; return its JSON text."""
        root = self._project()
        argv = [sys.executable, "-m", "lawkeep", command, *args, "--json"]
        if root is not None:
            argv += ["--root", str(root)]
        try:
            code, out, err = self.runner(argv, root, self.timeout)
        except subprocess.TimeoutExpired:
            return self._problem(command, f"`lawkeep {command}` did not finish in time.")
        except OSError as exc:
            return self._problem(command, f"could not start `lawkeep {command}`: {exc}")
        text = out.strip()
        try:
            json.loads(text)
        except ValueError:
            return self._problem(
                command,
                f"`lawkeep {command}` did not print a JSON report (result code {code}).",
                stderr=err[-2000:],
            )
        return text

    @staticmethod
    def _problem(command: str, message: str, **data: Any) -> str:
        report = SimpleReport(
            kind=command,
            exit_code=exitcodes.SETUP_PROBLEM,
            summary=f"SETUP PROBLEM: {message}",
            data=data,
        )
        return json.dumps(to_jsonable(report), indent=2, ensure_ascii=False)

    # -- tools ----------------------------------------------------------------------------------

    def check(self, profile: str | None = None, laws: list[str] | None = None) -> str:
        args: list[str] = []
        if profile:
            args += ["--profile", profile]
        for law_id in laws or []:
            args += ["--law", law_id]
        return self.run_cli("check", *args)

    def propose(
        self,
        law_id: str,
        reason: str,
        file: str | None = None,
        content: str | None = None,
        delete: bool = False,
        actor: str | None = None,
    ) -> str:
        if not file and content is not None:
            root = self._project()
            if root is None:
                return self._problem("propose", "no lawkeep project found (run `lawkeep init`).")
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", law_id) or "law"
            target = paths.drafts_dir(root) / "proposed" / f"{safe}.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            file = paths.rel(root, target)
        if not file and not delete:
            return self._problem(
                "propose",
                "give the new law version as `file` (a path under laws/drafts/) or `content`.",
            )
        who = actor or self.actor
        if not who.startswith("agent:"):  # proposals over MCP always come from an agent
            who = "agent:" + (who.split(":", 1)[-1] or "other")
        args = [law_id, "--reason", reason, "--actor", who]
        if file:
            args += ["--file", file]
        if delete:
            args.append("--delete")
        return self.run_cli("propose", *args)

    def report(self, since: str | None = None, session: bool = True) -> str:
        args: list[str] = []
        if since:
            args += ["--since", since]
        elif session:
            args.append("--session")
        return self.run_cli("report", *args)

    def list_laws(self, include_drafts: bool = False) -> str:
        root = self._project()
        if root is None:
            return self._problem("list_laws", "no lawkeep project found (run `lawkeep init`).")
        from lawkeep import discovery

        found = discovery.discover_laws(root, include_drafts=include_drafts)
        n = len(found.laws)
        report = SimpleReport(
            kind="list_laws",
            summary=f"{n} law{'s' if n != 1 else ''} found"
            + (" (including drafts)" if include_drafts else ""),
            lines=[
                f"{law.law_id}: {law.sentence or '(sentence not readable)'}" for law in found.laws
            ],
            data={
                "root": str(root),
                "laws": [law.to_dict() for law in found.laws],
                "problems": list(found.problems),
            },
        )
        return json.dumps(to_jsonable(report), indent=2, ensure_ascii=False)


TOOL_DESCRIPTIONS = {
    "check": (
        "Run every law in laws/ (lawkeep check). Returns the gate's JSON report: ALL LAWS HOLD or "
        "SOME LAWS FAIL, each failure with its simplest failing example. Call it before finishing. "
        "Optional: profile ('pr' or 'deep'), laws (list of law ids)."
    ),
    "propose": (
        "Propose a change to a law (lawkeep propose) instead of editing laws/ directly. Give the "
        "law id, a reason (the person's request or why), and the new version as `file` (path "
        "under laws/drafts/) or `content` (full Python source). lawkeep decides by evidence "
        "whether it is stricter or looser: stricter may apply now, weakening waits for the "
        "person's yes (exit_code 2 = waiting: tell the person; if nobody is there, follow "
        "`next_step`: keep going with work that does not depend on this law and list the "
        "`history_id` in your final summary). `delete=true` proposes removal."
    ),
    "report": (
        "The law change report (lawkeep report): every law change in this session (or since "
        "`since`, an ISO time), anything changed outside lawkeep, and what the laws do not "
        "protect. Show it to the person at the end of the session."
    ),
    "list_laws": (
        "List the project's laws (id, plain-English sentence, file) without running any code. "
        "include_drafts=true also lists drafts in laws/drafts/."
    ),
}


class _Server(Protocol):
    """The part of the MCP SDK's server (``MCPServer`` / ``FastMCP``) that lawkeep uses."""

    def tool(
        self, *, name: str | None = ..., description: str | None = ...
    ) -> Callable[[Callable[..., str]], object]: ...

    def run(self, transport: Literal["stdio"]) -> object: ...


class _ServerClass(Protocol):
    def __call__(self, *, name: str, instructions: str) -> _Server: ...


def _import_server_class() -> _ServerClass:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return MCPServer
    except ImportError:
        pass
    try:
        # mcp 1.x. Imported by name so the type checker, which sees the locked mcp 2.x,
        # does not need it; the class has the same tool()/run() interface as MCPServer.
        fastmcp = importlib.import_module("mcp.server.fastmcp")
        return cast("_ServerClass", fastmcp.FastMCP)
    except ImportError:
        raise SetupError(
            "`lawkeep mcp` needs the optional `mcp` package, which is not installed.",
            hint=INSTALL_HINT,
        ) from None


def build_server(
    root: Path | None = None, *, actor: str = "agent:other", tools: LawkeepTools | None = None
) -> _Server:
    """Create the MCP server object with lawkeep's four tools registered (does not start it)."""
    server_cls = _import_server_class()
    impl = tools or LawkeepTools(root, actor=actor)
    server = server_cls(name="lawkeep", instructions=INSTRUCTIONS)

    def check(profile: str | None = None, laws: list[str] | None = None) -> str:
        return impl.check(profile=profile, laws=laws)

    def propose(
        law_id: str,
        reason: str,
        file: str | None = None,
        content: str | None = None,
        delete: bool = False,
    ) -> str:
        return impl.propose(law_id, reason, file=file, content=content, delete=delete)

    def report(since: str | None = None, session: bool = True) -> str:
        return impl.report(since=since, session=session)

    def list_laws(include_drafts: bool = False) -> str:
        return impl.list_laws(include_drafts=include_drafts)

    for fn in (check, propose, report, list_laws):
        server.tool(name=fn.__name__, description=TOOL_DESCRIPTIONS[fn.__name__])(fn)
    return server


def serve(root: Path | None = None, *, actor: str | None = None) -> int:
    """Run the server on stdio until the client disconnects. Returns a result code."""
    actor = actor or os.environ.get("LAWKEEP_ACTOR") or "agent:other"
    server = build_server(root, actor=actor)
    server.run("stdio")
    return exitcodes.ALL_HOLD
