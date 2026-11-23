"""Tests for lawkeep.cli: discovery of command modules, --json/--root, result codes."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

import lawkeep
from lawkeep import cli
from lawkeep.errors import SetupError


def test_every_command_module_has_the_interface():
    import importlib

    for name in cli.COMMANDS:
        module = importlib.import_module(f"lawkeep.commands.{name}")
        assert callable(module.add_arguments), name
        assert callable(module.run), name


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for name in cli.COMMANDS:
        assert name in out


def test_no_command_prints_help_and_returns_3(capsys):
    assert cli.main([]) == 3
    assert "usage" in capsys.readouterr().out


def test_bad_usage_exits_3_not_2(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["no-such-command"])
    assert info.value.code == 3
    with pytest.raises(SystemExit) as info:
        cli.main(["check", "--no-such-flag"])
    assert info.value.code == 3


def _fake(monkeypatch, run):
    module = types.SimpleNamespace(
        HELP="fake", add_arguments=lambda p: p.add_argument("--x"), run=run
    )
    real = cli._load
    monkeypatch.setattr(
        cli, "_load", lambda name: (module, None) if name == "check" else real(name)
    )


def test_dispatch_passes_common_args(monkeypatch):
    seen = {}

    def run(args):
        seen.update(json=args.json, root=args.root, x=args.x)
        return 1

    _fake(monkeypatch, run)
    assert cli.main(["check", "--json", "--root", "/tmp/p", "--x", "7"]) == 1
    assert seen == {"json": True, "root": "/tmp/p", "x": "7"}


def test_setup_error_becomes_code_3(monkeypatch, capsys):
    def run(args):
        raise SetupError("No laws/ folder.", hint="Run lawkeep init.")

    _fake(monkeypatch, run)
    assert cli.main(["check", "--json"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert (
        data["summary"] == "SETUP PROBLEM: No laws/ folder." and data["hint"] == "Run lawkeep init."
    )


def test_crash_becomes_code_3(monkeypatch, capsys):
    monkeypatch.delenv("LAWKEEP_DEBUG", raising=False)

    def run(args):
        raise RuntimeError("boom")

    _fake(monkeypatch, run)
    assert cli.main(["check"]) == 3
    assert "internal error" in capsys.readouterr().out


def test_invalid_return_value(monkeypatch):
    _fake(monkeypatch, lambda args: 42)
    assert cli.main(["check", "--json"]) == 3


def test_broken_command_module_does_not_break_cli(monkeypatch, capsys):
    real = cli._load
    monkeypatch.setattr(
        cli, "_load", lambda n: (None, ImportError("bad")) if n == "draft" else real(n)
    )
    monkeypatch.delenv("LAWKEEP_DEBUG", raising=False)
    assert cli.main(["draft", "--json"]) == 3
    assert "failed to load" in json.loads(capsys.readouterr().out)["summary"]


@pytest.mark.parametrize("name", cli.COMMANDS)
def test_every_command_answers_json_via_console(name, tmp_path):
    """`lawkeep <cmd> --json` with no arguments prints exactly one JSON document of its kind.

    Runs in an empty temporary folder (never the repository itself: `init` would install
    lawkeep there, and `draft` could start a coding agent) with stdin closed.
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "LAWKEEP_ROOT"}
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", name, "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    if name == "mcp":
        # stdout carries the MCP protocol; with stdin closed the server simply stops.
        assert proc.returncode in (0, 3)
        return
    data = json.loads(proc.stdout)
    assert data["exit_code"] == proc.returncode
    assert proc.returncode in (0, 1, 2, 3)
    assert data["kind"] == name


def test_version():
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", "--version"], capture_output=True, text=True
    )
    assert proc.returncode == 0 and lawkeep.__version__ in proc.stdout
