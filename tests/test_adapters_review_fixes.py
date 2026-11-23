"""Tests for the v0.1 review fixes in the adapters: workflow protection, indirect approvals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lawkeep import cli, lock
from lawkeep.adapters import guard


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "laws").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "lawkeep.toml").write_text("", encoding="utf-8")
    return root


def bash(root: Path, command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(root)}


def test_ci_workflow_is_protected_from_direct_edits(tmp_path):
    root = make_project(tmp_path)
    wf = root / ".github" / "workflows" / "lawkeep.yml"
    d = guard.decide(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(wf), "content": "x"},
            "cwd": str(root),
        }
    )
    assert d.action == "deny" and ".github/workflows/lawkeep.yml" in d.paths


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"import subprocess; subprocess.run(['lawkeep','approve','--yes'])\"",
        'python -c \'from lawkeep.cli import main; main(["approve", "--yes"])\'',
        'python -c \'from lawkeep import history; history.record_change(".", action="x")\'',
    ],
)
def test_wrapped_approvals_still_ask_the_person(tmp_path, command):
    root = make_project(tmp_path)
    assert guard.decide(bash(root, command)).action == "ask"


def test_plain_commands_are_not_asked(tmp_path):
    root = make_project(tmp_path)
    assert guard.decide(bash(root, "lawkeep check --json")).action == "allow"


def test_init_ci_fingerprints_the_workflow(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    code = cli.main(["init", "--root", str(root), "--agent", "none", "--ci", "--json"])
    capsys.readouterr()
    assert code == 0
    locked = lock.read_lock(root)
    assert locked is not None
    assert lock.CI_WORKFLOW in locked.files
    wf = root / lock.CI_WORKFLOW
    wf.write_text(wf.read_text() + "\n# made to always pass\n", encoding="utf-8")
    assert lock.CI_WORKFLOW in lock.diff_against_lock(root).paths()
    code = cli.main(["check", "--root", str(root), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert any(
        i["kind"] == "unrecorded_law_change" and i["path"] == lock.CI_WORKFLOW
        for i in report["integrity"]
    ), report["integrity"]
