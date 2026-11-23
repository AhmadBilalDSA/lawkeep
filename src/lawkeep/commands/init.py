"""`lawkeep init`: set up lawkeep in this project.

Owner: workstream E (adapters).

``lawkeep init [--agent claude-code|codex|cursor|copilot|all|other|none ...] [--no-hooks]
[--ci | --no-ci] [--json]``

Creates (only what is missing; never overwrites):

- ``lawkeep.toml`` (``config.render_default_toml``), ``laws/``, ``laws/drafts/``,
  ``laws/strategies.py``, empty ``laws/history.jsonl`` and ``laws/failing_examples.jsonl``;
- the first ``laws.lock`` plus an ``init`` entry in the law history
  (``history.record_change(action="init")``);
- ``.lawkeep/`` in ``.gitignore``;
- the shared base: the lawkeep section in ``AGENTS.md`` and the git pre-commit hook;
- the adapter for each chosen agent (``--agent``; without it, the agents whose folders are
  present: ``.claude/`` or ``CLAUDE.md``, ``.codex/``, ``.cursor/``, ``.github/copilot-instructions.md``...);
- the GitHub Action workflow ``.github/workflows/lawkeep.yml`` with ``--ci`` (default: only when
  the project already has a ``.github/`` folder; ``--no-ci`` never).

Safe to re-run. If laws changed outside lawkeep since the last record (or ``laws.lock`` was
deleted while the history has entries), init does not touch any law file and returns 2: that
needs a person's decision (``lawkeep approve --accept-unrecorded``).

Result codes: 0 done · 2 waiting for a person's decision (unrecorded law change) · 3 problem.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import RenderableType

from lawkeep import config, exitcodes, history, lock, paths
from lawkeep.adapters import base
from lawkeep.errors import SetupError
from lawkeep.models import SimpleReport
from lawkeep.output import emit

HELP = "set up lawkeep in this project (safe to run again)"

AGENT_CHOICES = (*base.AGENT_ORDER, "all", "other", "none")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        metavar="NAME",
        help="coding agent(s) to set up: claude-code, codex, cursor, copilot, all, other or none "
        "(repeat or comma-separate; default: the agents found in this folder)",
    )
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="do not install the git pre-commit hook or the agent hooks (only the "
        "instructions and the MCP server)",
    )
    ci = parser.add_mutually_exclusive_group()
    ci.add_argument(
        "--ci",
        dest="ci",
        action="store_true",
        default=None,
        help="also check the laws on every pull request (adds a GitHub Action workflow)",
    )
    ci.add_argument(
        "--no-ci", dest="ci", action="store_false", help="do not add the GitHub Action workflow"
    )


def _chosen_agents(root: Path, requested: list[str] | None) -> tuple[list[str], bool, bool]:
    """``(agents, shared_base, detected)``: adapters to install, whether to add AGENTS.md."""
    if not requested:
        found = base.detect_agents(root)
        return found, True, True
    names: list[str] = []
    for item in requested:
        names.extend(n.strip().lower() for n in item.split(",") if n.strip())
    bad = [n for n in names if n not in AGENT_CHOICES]
    if bad:
        raise SetupError(
            f"Unknown agent name(s): {', '.join(bad)}.",
            hint="Choose from: "
            + ", ".join(AGENT_CHOICES)
            + ". Example: lawkeep init --agent claude-code",
        )
    if "none" in names:
        if len(set(names)) > 1:
            raise SetupError("`--agent none` cannot be combined with other agents.")
        return [], False, False
    if "all" in names:
        return list(base.AGENT_ORDER), True, False
    agents = [a for a in base.AGENT_ORDER if a in names]
    return agents, True, False


def _core_files(root: Path, cfg_agent: str, created: list[str]) -> None:
    """Create the lawkeep files that are missing (lawkeep.toml, laws/ ...)."""
    cfg_path = paths.config_path(root)
    if not cfg_path.exists():
        source = ["src"] if (root / "src").is_dir() else None
        cfg_path.write_text(
            config.render_default_toml(agent_name=cfg_agent, source=source), encoding="utf-8"
        )
        created.append(paths.CONFIG_FILENAME)
    for folder in (paths.laws_dir(root), paths.drafts_dir(root)):
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(paths.rel(root, folder) + "/")
    strategies = paths.strategies_path(root)
    if not strategies.exists():
        strategies.write_text(base.template("strategies.py"), encoding="utf-8")
        created.append(paths.rel(root, strategies))
    for f in (paths.history_path(root), paths.ledger_path(root)):
        if not f.exists():
            f.touch()
            created.append(paths.rel(root, f))


def _lock_state(root: Path) -> tuple[str, list[str]]:
    """``("new" | "clean" | "unrecorded" | "lock_lost", paths)`` before init changes anything."""
    current = lock.read_lock(root)  # LockError (code 3) when laws.lock is unreadable
    if current is None:
        try:
            entries = history.read(root) if paths.history_path(root).exists() else []
        except Exception:
            entries = [None]  # unreadable history: treat as "there was history"
        return ("lock_lost", []) if entries else ("new", [])
    diff = lock.compare(current, lock.fingerprint(root))
    return ("clean", []) if diff.is_empty else ("unrecorded", diff.paths())


def _want_ci(root: Path, flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return (root / ".github").is_dir()


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser() if args.root else Path.cwd()
    if not root.is_dir():
        raise SetupError(
            f"{root} is not a folder.", hint="Run `lawkeep init` inside your project folder."
        )
    root = root.resolve()
    agents, shared, detected = _chosen_agents(root, args.agent)
    hooks = not args.no_hooks
    lines: list[str] = []
    notes: list[str] = []
    created: list[str] = []
    parent = paths.find_root(root.parent) if root.parent != root else None
    if parent is not None and not paths.config_path(root).exists():
        notes.append(
            f"Note: {parent} above this folder is already a lawkeep project; this folder gets its own."
        )

    # 1. Law files, the GitHub Action workflow (fingerprinted in laws.lock like a law file)
    #    and the first record (only when nothing changed outside lawkeep).
    state, unrecorded = _lock_state(root)
    code = exitcodes.ALL_HOLD
    shared_result = base.InstallResult(agent="shared")
    ci_added = False
    if state in ("new", "clean"):
        cfg_agent = agents[0] if agents else ""
        _core_files(root, cfg_agent, created)
        if _want_ci(root, args.ci):
            wf = root / ".github" / "workflows" / "lawkeep.yml"
            ci_added = base.write_if_missing(
                root, wf, base.template("lawkeep-workflow.yml"), shared_result
            )
        if state == "new" or not lock.diff_against_lock(root).is_empty:
            change = history.record_change(
                root,
                action="init",
                actor=history.default_person(root),
                direction="stricter",
                reason="lawkeep init" if state == "new" else "lawkeep init: added missing files",
                details={"agents": agents},
            )
            created.append(
                paths.LOCK_FILENAME if state == "new" else f"{paths.LOCK_FILENAME} (updated)"
            )
            notes.extend(change.notes if state != "new" else [])
    elif state == "unrecorded":
        code = exitcodes.WAITING
        shown = ", ".join(unrecorded[:5]) + (" ..." if len(unrecorded) > 5 else "")
        notes.append(
            f"Some law files changed without going through lawkeep ({shown}), so init did not "
            "touch your laws. Review the change, then accept it with "
            "`lawkeep approve --accept-unrecorded` (or undo it), and run `lawkeep init` again."
        )
    else:  # lock_lost
        code = exitcodes.WAITING
        notes.append(
            "laws.lock is missing but the law history has entries, so init did not recreate it "
            "(that would hide any law change made meanwhile). Restore laws.lock from git, or "
            "run `lawkeep check` to see what changed."
        )

    cfg = config.load_config(root)
    notes.extend(cfg.warnings)

    # 2. Local state stays out of git.
    base.ensure_gitignore(root, f"{paths.STATE_DIRNAME}/", shared_result)

    # 3. Shared base + adapters.
    if shared:
        base.install_agents_md(root, shared_result)
    if hooks:
        base.install_pre_commit_hook(root, shared_result)
    results = [shared_result]
    for name in agents:
        results.append(base.get_adapter(name).install(root, hooks=hooks))

    # 5. Plain-English summary.
    changed = list(dict.fromkeys([*created, *(f for r in results for f in r.files_changed)]))
    if changed:
        lines.append("lawkeep set up this project. Created or updated:")
        lines.extend(f"  + {f}" for f in changed)
    else:
        lines.append("lawkeep is already set up here; nothing needed changing.")
    if agents:
        lines.append(
            "Coding agents set up: "
            + ", ".join(agents)
            + (" (found in this folder)" if detected else "")
        )
    elif detected:
        lines.append(
            "No coding agent's settings were found here, so only the shared setup was added "
            "(AGENTS.md, which most agents read). To add one: lawkeep init --agent claude-code "
            "(or codex, cursor, copilot, all)."
        )
    for r in results:
        if r.files_changed:
            notes.extend(r.notes)
        notes.extend(r.warnings)
    if (
        not ci_added
        and args.ci is None
        and not (root / ".github" / "workflows" / "lawkeep.yml").exists()
    ):
        notes.append("To check laws on every pull request too, run: lawkeep init --ci")
    lines.extend(f"Note: {n}" if not n.startswith("Note:") else n for n in dict.fromkeys(notes))
    next_step = (
        'Next: ask your coding agent to draft laws, for example "add a law that refunds never '
        'exceed the payment" (it runs `lawkeep draft`), then approve them with `lawkeep approve`.'
    )
    summary = (
        "lawkeep is set up."
        if code == exitcodes.ALL_HOLD
        else "lawkeep set up the agent files, but your laws need a person's decision first."
    )
    report = SimpleReport(
        kind="init",
        exit_code=code,
        summary=summary,
        lines=lines,
        hint=next_step,
        data={
            "root": str(root),
            "state": state,
            "unrecorded": unrecorded,
            "agents": agents,
            "agents_detected": detected,
            "hooks": hooks,
            "ci_workflow_added": ci_added,
            "files_changed": changed,
            "adapters": [r.to_dict() for r in results],
            "notes": list(dict.fromkeys(notes)),
            "next_step": next_step,
        },
    )
    text = None
    if not args.json:
        text = _screen(code, changed, agents, detected, list(dict.fromkeys(notes)), next_step)
    emit(report, args.json, text=text)
    return report.exit_code


def _screen(
    code: int,
    changed: list[str],
    agents: list[str],
    detected: bool,
    notes: list[str],
    next_step: str,
) -> str:
    """The human screen for `lawkeep init` (docs/OUTPUT-STYLE.md)."""
    from lawkeep import ui

    what = (
        "lawkeep keeps laws — rules your code must always follow — in laws/, and "
        "`lawkeep check` tells you whether your code follows all of them."
    )
    if code != exitcodes.ALL_HOLD:
        banner = ui.banner(
            "WAITING FOR YOUR DECISION",
            "lawkeep set up the agent files, but your laws need a person's decision first, so "
            "it did not touch them.",
            tone="wait",
        )
    elif changed:
        banner = ui.banner("LAWKEEP IS SET UP", what, tone="ok")
    else:
        banner = ui.banner(
            "ALREADY SET UP", "lawkeep is already set up here; nothing needed changing.", tone="ok"
        )
    out: list[RenderableType] = [banner]
    if changed:
        out += [ui.blank(), ui.bold("Files lawkeep created or updated")]
        out += [ui.indent(ui.dim(f)) for f in changed]
    if agents:
        found = " (found in this folder)" if detected else ""
        out += [ui.blank(), ui.status("ok", f"Coding agents set up{found}: {', '.join(agents)}")]
    elif detected:
        out += [
            ui.blank(),
            ui.status(
                "info",
                "No coding agent's settings were found here, so only the shared setup was added "
                "(AGENTS.md, which most agents read). To add one: `lawkeep init --agent "
                "claude-code` (or codex, cursor, copilot, all).",
            ),
        ]
    for n in notes:
        n = n.removeprefix("Note: ")
        if "need" in n and "decision" in n or "did not" in n:
            out.append(ui.warning(n))
        else:
            out.append(ui.status("info", n))
    if code != exitcodes.ALL_HOLD:
        step = (
            "a person decides about the changed law files (`lawkeep approve "
            "--accept-unrecorded`), then run `lawkeep init` again."
        )
    else:
        step = (
            "ask your coding agent to draft laws, for example “add a law that refunds never "
            "exceed the payment” (it runs `lawkeep draft`). Then read and approve them with "
            "`lawkeep approve`."
        )
    out += [ui.blank(), ui.next_step(step)]
    return ui.render(*out)
