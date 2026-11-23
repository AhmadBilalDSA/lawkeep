"""Workstream E: `lawkeep hook` speaks each agent's hook format."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lawkeep import cli, history


def make_project(tmp_path: Path, *, git: bool = False) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Ada"], check=True)
    code = cli.main(["init", "--root", str(root), "--agent", "none", "--json"])
    assert code == 0
    return root


def run_hook(monkeypatch, capsys, args: list[str], payload) -> tuple[int, str, str]:
    capsys.readouterr()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = cli.main(["hook", *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def edit_payload(root: Path, rel: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root / rel), "old_string": "a", "new_string": "b"},
        "cwd": str(root),
    }


def test_claude_hook_denies_edit_to_law_and_allows_src(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    code, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["pre-tool-use", "--agent", "claude-code"],
        edit_payload(root, "laws/x.py"),
    )
    assert code == 0
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "PreToolUse"
    assert data["permissionDecision"] == "deny"
    assert "lawkeep propose" in data["permissionDecisionReason"]

    code, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["pre-tool-use", "--agent", "claude-code"],
        edit_payload(root, "src/x.py"),
    )
    assert code == 0 and out == ""  # no output = no decision, normal permission flow


def test_agent_first_form(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    code, out, _ = run_hook(monkeypatch, capsys, ["claude-code"], edit_payload(root, "laws.lock"))
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_hook_asks_for_approve(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "lawkeep approve h-1 --yes"},
        "cwd": str(root),
    }
    _, out, _ = run_hook(monkeypatch, capsys, ["pre-tool-use", "--agent", "claude-code"], payload)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_codex_formats(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    patch = "*** Begin Patch\n*** Add File: laws/weak.py\n+x\n*** End Patch"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch}, "cwd": str(root)}
    _, out, _ = run_hook(monkeypatch, capsys, ["pre-tool-use", "--agent", "codex"], payload)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    approve = {
        "tool_name": "Bash",
        "tool_input": {"command": "lawkeep approve h-1"},
        "cwd": str(root),
    }
    _, out, _ = run_hook(monkeypatch, capsys, ["pre-tool-use", "--agent", "codex"], approve)
    assert out == ""  # Codex hooks cannot ask; its rules file prompts instead


def test_cursor_formats(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    _, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["pre-tool-use", "--agent", "cursor"],
        {"command": "rm laws.lock", "cwd": str(root)},
    )
    data = json.loads(out)
    assert data["permission"] == "deny" and "lawkeep" in data["agent_message"]
    _, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["pre-tool-use", "--agent", "cursor"],
        {"command": "ls", "cwd": str(root)},
    )
    assert json.loads(out) == {"permission": "allow"}
    _, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["pre-tool-use", "--agent", "cursor"],
        {"command": "lawkeep approve h-1 --yes", "cwd": str(root)},
    )
    assert json.loads(out)["permission"] == "ask"


def test_copilot_format(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    payload = {
        "toolName": "edit",
        "toolArgs": json.dumps({"path": "laws/strategies.py"}),
        "cwd": str(root),
    }
    _, out, _ = run_hook(monkeypatch, capsys, ["pre-tool-use", "--agent", "copilot"], payload)
    data = json.loads(out)
    assert data["permissionDecision"] == "deny" and data["permissionDecisionReason"]


def test_pre_tool_use_json_mode(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    code, out, _ = run_hook(
        monkeypatch, capsys, ["pre-tool-use", "--json"], edit_payload(root, "laws/x.py")
    )
    data = json.loads(out)
    assert code == 0 and data["kind"] == "hook" and data["data"]["action"] == "deny"


@pytest.mark.parametrize("payload", ["", "not json", "[1, 2]"])
def test_bad_payload_never_blocks(monkeypatch, capsys, payload):
    code, out, _ = run_hook(monkeypatch, capsys, ["pre-tool-use"], payload)
    assert code == 0 and out == ""


def test_unknown_event_is_setup_problem(monkeypatch, capsys):
    code, out, _ = run_hook(monkeypatch, capsys, ["teleport", "--json"], "")
    assert code == 3 and json.loads(out)["kind"] == "hook"


def test_session_start_marks_and_reminds(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    code, out, _ = run_hook(
        monkeypatch,
        capsys,
        ["session-start", "--agent", "claude-code"],
        {"source": "startup", "cwd": str(root)},
    )
    assert code == 0 and "lawkeep check" in out and "lawkeep propose" in out
    first = history.session_start(root)
    assert first
    session = json.loads((root / ".lawkeep" / "session.json").read_text())
    assert session["agent"] == "claude-code"
    # a resumed session keeps its start time
    run_hook(monkeypatch, capsys, ["session-start"], {"source": "resume", "cwd": str(root)})
    assert history.session_start(root) == first
    # Cursor and Copilot get JSON
    _, out, _ = run_hook(
        monkeypatch, capsys, ["session-start", "--agent", "cursor"], {"cwd": str(root)}
    )
    assert "lawkeep check" in json.loads(out)["additional_context"]
    _, out, _ = run_hook(
        monkeypatch, capsys, ["session-start", "--agent", "copilot"], {"cwd": str(root)}
    )
    assert "lawkeep check" in json.loads(out)["additionalContext"]


def test_session_start_mentions_waiting_decisions(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    history.record_change(
        root, action="change_law", actor="agent:codex", law_id="r.x", status="waiting"
    )
    _, out, _ = run_hook(monkeypatch, capsys, ["session-start"], {"cwd": str(root)})
    assert "1 thing waits for your decision (1 proposed law change)" in out
    assert "run `lawkeep approve`" in out


def test_session_start_outside_a_project_does_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LAWKEEP_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    code, out, _ = run_hook(monkeypatch, capsys, ["session-start"], {"cwd": str(tmp_path)})
    assert code == 0 and out == ""


def test_session_end_prints_report(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path)
    code, out, _ = run_hook(monkeypatch, capsys, ["session-end", "--json"], {"cwd": str(root)})
    data = json.loads(out)
    assert code == 0 and data["kind"] == "hook" and data["data"]["event"] == "session-end"


def test_pre_commit_shows_but_does_not_block_by_default(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path, git=True)
    monkeypatch.delenv("LAWKEEP_PRECOMMIT", raising=False)
    code, out, err = run_hook(monkeypatch, capsys, ["pre-commit", "--root", str(root)], "")
    assert code == 0 and err == ""
    (root / "laws" / "strategies.py").write_text("# changed outside lawkeep\n")
    code, out, err = run_hook(monkeypatch, capsys, ["pre-commit", "--root", str(root)], "")
    assert code == 0
    assert "laws/strategies.py" in err and "lawkeep report" in err
    code, out, _ = run_hook(monkeypatch, capsys, ["pre-commit", "--root", str(root), "--json"], "")
    data = json.loads(out)
    assert data["data"]["blocking"] is False and data["data"]["issues"]


def test_pre_commit_blocks_only_when_asked(tmp_path, monkeypatch, capsys):
    root = make_project(tmp_path, git=True)
    (root / "laws" / "strategies.py").write_text("# changed outside lawkeep\n")
    subprocess.run(["git", "-C", str(root), "config", "lawkeep.precommit", "block"], check=True)
    code, _, err = run_hook(monkeypatch, capsys, ["pre-commit", "--root", str(root)], "")
    assert code == 1 and "lawkeep stopped this commit" in err


def test_pre_commit_outside_project(tmp_path, monkeypatch, capsys):
    code, out, err = run_hook(monkeypatch, capsys, ["pre-commit", "--root", str(tmp_path)], "")
    assert code == 0 and out == "" and err == ""


def test_pre_commit_uses_the_gate_integrity_check_even_with_broken_settings(
    tmp_path, monkeypatch, capsys
):
    """pre-commit runs the gate's integrity check (no second copy of it in the hook)."""
    root = make_project(tmp_path)
    (root / "laws" / "new_law.py").write_text("x = 1\n")
    (root / "lawkeep.toml").write_text("[check\n")  # broken TOML, edited by hand
    code, out, _ = run_hook(monkeypatch, capsys, ["pre-commit", "--json", "--root", str(root)], "")
    data = json.loads(out)
    kinds = {i["kind"] for i in data["data"]["issues"]}
    assert "unrecorded_law_added" in kinds and code == 0
    assert data["data"]["notes"]  # the settings problem is shown, not hidden


def test_hook_as_installed_in_claude_settings_runs_end_to_end(tmp_path):
    """Run the exact command string written into .claude/settings.json through a shell."""
    from lawkeep.adapters import claude_code

    root = make_project(tmp_path)
    venv_bin = tmp_path / "shop" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    shim = venv_bin / "lawkeep"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m lawkeep "$@"\n')
    shim.chmod(0o755)
    cmd = claude_code.hook_command("pre-tool-use")
    env = {"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(root)}
    payload = json.dumps(edit_payload(root, "laws/refunds.py"))
    proc = subprocess.run(["sh", "-c", cmd], input=payload, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_missing_event_is_setup_problem_with_json(monkeypatch, capsys):
    code, out, _ = run_hook(monkeypatch, capsys, ["--json"], "")
    data = json.loads(out)
    assert code == 3 and data["kind"] == "hook" and "pre-tool-use" in data["hint"]
