"""`lawkeep mcp`: run the lawkeep MCP server (check, propose, report, list_laws) for coding agents.

Owner: workstream E (adapters).

``lawkeep mcp [--agent NAME]`` starts :func:`lawkeep.mcp_server.serve` on stdio (needs the
``mcp`` extra). ``--agent`` sets who proposals are recorded as (``agent:<name>``). If the ``mcp``
package is missing it prints how to install it and returns 3. The server prints MCP protocol
messages on stdout, so ``--json`` only affects the "not installed" message.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from lawkeep import paths

HELP = "let coding agents use lawkeep as tools (check, propose, report, list_laws)"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help="the agent using the server, recorded as the actor of proposals "
        "(claude-code, codex, cursor, copilot; default: other)",
    )


def run(args: argparse.Namespace) -> int:
    from lawkeep import mcp_server

    root = paths.find_root(args.root) if args.root else paths.find_root()
    if args.root and root is None:
        root = Path(args.root)
    actor = f"agent:{args.agent}" if args.agent else None
    # A missing `mcp` package raises SetupError, which cli.py prints (with the install hint)
    # as a standard setup problem with result code 3.
    return mcp_server.serve(root, actor=actor)
