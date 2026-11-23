"""The ``lawkeep`` command line.

``cli.py`` never needs editing when a command changes. Each subcommand lives in
``lawkeep/commands/<name>.py`` and exposes:

    HELP: str                                   # one-line help (optional)
    def add_arguments(parser: argparse.ArgumentParser) -> None
    def run(args: argparse.Namespace) -> int    # returns a result code (lawkeep.exitcodes)

Every subcommand automatically gets ``--json`` (``args.json``) and ``--root PATH``
(``args.root``; ``None`` = search from the current folder, see ``lawkeep.paths.find_root``).
A ``SetupError`` raised by ``run`` becomes a plain-English message and result code 3; so does
an unexpected crash (set ``LAWKEEP_DEBUG=1`` to see the traceback). A command module that
fails to import does not break the other commands.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
from collections.abc import Sequence
from types import ModuleType
from typing import NoReturn

from lawkeep import __version__, exitcodes
from lawkeep.errors import LawkeepError
from lawkeep.output import setup_problem

#: Subcommands, in the order shown by ``lawkeep --help``.
COMMANDS: tuple[str, ...] = (
    "init",
    "draft",
    "approve",
    "check",
    "strength",
    "propose",
    "report",
    "hook",
    "mcp",
)

DEFAULT_HELP = {
    "init": "set up lawkeep in this project (safe to run again)",
    "draft": "ask your coding agent (in a separate session) to write draft laws for you to approve",
    "approve": "read the drafted laws and proposed law changes, and say yes or no to each",
    "check": "check that your code follows every law (answer: ALL LAWS HOLD or SOME LAWS FAIL)",
    "strength": "test whether your laws are strong enough to catch real mistakes in the code",
    "propose": "ask for a change to a law; changes that weaken a law wait for a person's yes",
    "report": "show every law change, anything waiting for you, and which changed code no law "
    "protects",
    "hook": "used by coding agents and git to call lawkeep (you do not need to run it)",
    "mcp": "let coding agents use lawkeep as tools (check, propose, report, list_laws)",
}


class LawkeepArgumentParser(argparse.ArgumentParser):
    """argparse exits with 2 on bad usage, but 2 means WAITING in lawkeep: use 3 instead."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(exitcodes.SETUP_PROBLEM, f"{self.prog}: error: {message}\n")


def _common_parent() -> LawkeepArgumentParser:
    parent = LawkeepArgumentParser(add_help=False)
    parent.add_argument(
        "--json",
        action="store_true",
        help="print one JSON document instead (for agents and scripts)",
    )
    parent.add_argument(
        "--root",
        metavar="PATH",
        default=None,
        help="project folder (default: search upward from the current folder)",
    )
    return parent


def _load(name: str) -> tuple[ModuleType | None, BaseException | None]:
    try:
        return importlib.import_module(f"lawkeep.commands.{name}"), None
    except Exception as exc:  # a broken command must not break the CLI
        return None, exc


def build_parser() -> argparse.ArgumentParser:
    parser = LawkeepArgumentParser(
        prog="lawkeep",
        description=(
            "lawkeep keeps laws: rules your code must always follow, each written as one plain "
            "sentence plus a check. Your coding agent never changes a law silently, and a "
            "change that weakens a law needs your yes. Start with `lawkeep init`, then "
            "`lawkeep check`. Result codes: 0 all laws hold, 1 some laws fail, 2 waiting for "
            "a person's decision, 3 setup problem."
        ),
    )
    parser.add_argument("--version", action="version", version=f"lawkeep {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    parent = _common_parent()
    for name in COMMANDS:
        module, error = _load(name)
        help_text = getattr(module, "HELP", None) or DEFAULT_HELP.get(name, "")
        if error is not None:
            help_text = f"{help_text} (failed to load)"
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[parent])
        if module is not None:
            try:
                module.add_arguments(p)
            except Exception as exc:  # pragma: no cover - defensive
                error = exc
        p.set_defaults(_command=name, _module=module, _load_error=error)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return exitcodes.SETUP_PROBLEM
    name = args._command
    as_json = bool(getattr(args, "json", False))
    if args._load_error is not None or args._module is None:
        if os.environ.get("LAWKEEP_DEBUG") and args._load_error is not None:
            raise args._load_error
        return setup_problem(
            name,
            f"the `{name}` command failed to load: {args._load_error}",
            as_json,
            hint="This is a bug in lawkeep. Set LAWKEEP_DEBUG=1 to see details.",
        )
    try:
        code = args._module.run(args)
    except LawkeepError as exc:
        return setup_problem(name, exc.message, as_json, hint=exc.hint)
    except KeyboardInterrupt:
        return exitcodes.SETUP_PROBLEM
    except Exception as exc:
        if os.environ.get("LAWKEEP_DEBUG"):
            raise
        return setup_problem(
            name,
            f"lawkeep hit an internal error while running `{name}`: {exc!r}",
            as_json,
            hint="This is a bug in lawkeep. Set LAWKEEP_DEBUG=1 to see the traceback.",
            data={"traceback": traceback.format_exc()},
        )
    if not isinstance(code, int) or code not in exitcodes.VERDICTS:
        return setup_problem(
            name, f"the `{name}` command returned an invalid result {code!r}.", as_json
        )
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
