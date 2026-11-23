"""Workstream E: adapters merge into existing agent settings without clobbering them."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lawkeep.adapters import base, claude_code, codex, copilot, cursor
from lawkeep.errors import SetupError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- shared base ------------------------------------------------------------------------------


def test_agents_md_snippet_is_idempotent(tmp_path):
    r = base.InstallResult("shared")
    assert base.install_agents_md(tmp_path, r) == "created"
    text = (tmp_path / "AGENTS.md").read_text()
    assert base.BEGIN_MARK in text and base.END_MARK in text
    for phrase in ("lawkeep check", "lawkeep propose", "stop and ask the person", "lawkeep report"):
        assert phrase in text
    r2 = base.InstallResult("shared")
    assert base.install_agents_md(tmp_path, r2) == "unchanged"
    assert (tmp_path / "AGENTS.md").read_text() == text and r2.files_changed == []


def test_agents_md_appends_to_existing_file_and_keeps_it(tmp_path):
    original = "# My project\n\nUse tabs.\n"
    (tmp_path / "AGENTS.md").write_text(original)
    assert base.install_agents_md(tmp_path, base.InstallResult("shared")) == "added"
    text = (tmp_path / "AGENTS.md").read_text()
    assert text.startswith(original)
    assert text.count(base.BEGIN_MARK) == 1
    assert base.install_agents_md(tmp_path, base.InstallResult("shared")) == "unchanged"
    assert (tmp_path / "AGENTS.md").read_text().count(base.BEGIN_MARK) == 1


def test_agents_md_keeps_a_section_the_person_edited(tmp_path):
    base.install_agents_md(tmp_path, base.InstallResult("shared"))
    p = tmp_path / "AGENTS.md"
    edited = p.read_text().replace("Before you finish any task", "Before you finish EVERY task")
    p.write_text(edited)
    r = base.InstallResult("shared")
    assert base.install_agents_md(tmp_path, r) == "kept"
    assert p.read_text() == edited
    assert r.warnings and "kept yours" in r.warnings[0]


def test_pre_commit_hook_installed_once(tmp_path):
    git_init(tmp_path)
    r = base.InstallResult("shared")
    assert base.install_pre_commit_hook(tmp_path, r) == "created"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert base.is_executable(hook)
    text = hook.read_text()
    assert text.startswith("#!/bin/sh") and "lawkeep hook pre-commit" in text
    assert base.install_pre_commit_hook(tmp_path, base.InstallResult("shared")) == "unchanged"
    assert hook.read_text() == text


def test_pre_commit_hook_added_to_existing_shell_hook(tmp_path):
    git_init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nruff check .\n")
    assert base.install_pre_commit_hook(tmp_path, base.InstallResult("shared")) == "added"
    text = hook.read_text()
    assert text.startswith("#!/bin/sh\nruff check .\n") and base.SH_BEGIN in text


def test_pre_commit_hook_not_touched_when_unsafe(tmp_path):
    git_init(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    r = base.InstallResult("shared")
    assert base.install_pre_commit_hook(tmp_path, r) == "skipped"
    assert hook.read_text() == "#!/usr/bin/env python3\nprint('hi')\n"
    assert "lawkeep hook pre-commit" in r.warnings[0]


def test_pre_commit_hook_without_git(tmp_path):
    r = base.InstallResult("shared")
    assert base.install_pre_commit_hook(tmp_path, r) == "no_git"
    assert "not a git repository" in r.warnings[0]


def test_pre_commit_script_runs_and_does_not_block_by_default(tmp_path):
    """The installed shell block exits 0 when lawkeep is not found (never breaks commits)."""
    git_init(tmp_path)
    base.install_pre_commit_hook(tmp_path, base.InstallResult("shared"))
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    proc = subprocess.run(
        ["sh", str(hook)], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, capture_output=True
    )
    assert proc.returncode == 0


def test_gitignore_entry_once(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules")
    r = base.InstallResult("shared")
    assert base.ensure_gitignore(tmp_path, ".lawkeep/", r)
    assert not base.ensure_gitignore(tmp_path, ".lawkeep/", r)
    text = (tmp_path / ".gitignore").read_text()
    assert text.startswith("node_modules\n") and text.count(".lawkeep/") == 1


def test_invalid_json_settings_are_never_overwritten(tmp_path):
    p = tmp_path / ".claude" / "settings.json"
    p.parent.mkdir()
    p.write_text("{not json")
    with pytest.raises(SetupError):
        claude_code.ADAPTER.install(tmp_path)
    assert p.read_text() == "{not json"


def test_registry_and_detection(tmp_path):
    assert [a.name for a in base.all_adapters()] == list(base.AGENT_ORDER)
    assert base.detect_agents(tmp_path) == []
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "copilot-instructions.md").write_text("hi")
    assert base.detect_agents(tmp_path) == ["claude-code", "codex", "cursor", "copilot"]
    with pytest.raises(SetupError):
        base.get_adapter("vim")


def test_launcher_runs_lawkeep_from_venv_or_does_nothing(tmp_path):
    cmd = base.launcher("hook pre-tool-use --agent claude-code", "$ROOT")
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    fake = venv / "lawkeep"
    fake.write_text('#!/bin/sh\necho "fake lawkeep $*"\n')
    fake.chmod(0o755)
    env = {"PATH": "/usr/bin:/bin", "ROOT": str(tmp_path)}
    out = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True)
    assert (
        out.returncode == 0 and "fake lawkeep hook pre-tool-use --agent claude-code" in out.stdout
    )
    fake.unlink()
    out = subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == ""


# --- Claude Code ------------------------------------------------------------------------------


def test_claude_settings_merge_preserves_user_settings(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    user = {
        "model": "opus",
        "env": {"FOO": "1"},
        "permissions": {
            "allow": ["Bash(npm test)"],
            "ask": ["Bash(git push *)"],
            "deny": ["Read(.env)"],
        },
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-guard.sh"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
        },
    }
    settings.write_text(json.dumps(user))
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {"command": "db-mcp"}}}))
    (tmp_path / "CLAUDE.md").write_text("# Notes\nBe brief.\n")

    r = claude_code.ADAPTER.install(tmp_path)
    data = load(settings)
    assert data["model"] == "opus" and data["env"] == {"FOO": "1"}
    assert data["permissions"]["allow"] == ["Bash(npm test)"]
    assert data["permissions"]["deny"] == ["Read(.env)"]
    assert data["permissions"]["ask"][0] == "Bash(git push *)"
    assert "Bash(lawkeep approve *)" in data["permissions"]["ask"]
    assert "Bash(uv run lawkeep approve *)" in data["permissions"]["ask"]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["hooks"][0]["command"] == "my-guard.sh"
    assert pre[1]["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash"
    assert pre[1]["hooks"][0]["type"] == "command"
    assert "lawkeep hook pre-tool-use --agent claude-code" in pre[1]["hooks"][0]["command"]
    assert "lawkeep hook session-start" in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert data["hooks"]["Stop"] == user["hooks"]["Stop"]
    mcp = load(tmp_path / ".mcp.json")
    assert mcp["mcpServers"]["db"] == {"command": "db-mcp"}
    entry = mcp["mcpServers"]["lawkeep"]
    assert entry["command"] == "sh" and entry["args"][0] == "-c"
    assert "exec lawkeep mcp" in entry["args"][1]
    assert '"${CLAUDE_PROJECT_DIR:-.}/.venv/bin/lawkeep"' in entry["args"][1]
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert claude_md.startswith("# Notes\nBe brief.\n") and "@AGENTS.md" in claude_md
    assert set(r.files_changed) == {".claude/settings.json", ".mcp.json", "CLAUDE.md"}

    # idempotent: nothing duplicated, nothing rewritten
    before = {p: p.read_text() for p in (settings, tmp_path / ".mcp.json", tmp_path / "CLAUDE.md")}
    r2 = claude_code.ADAPTER.install(tmp_path)
    assert r2.files_changed == []
    assert {p: p.read_text() for p in before} == before


def test_claude_md_with_import_is_left_alone(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
    claude_code.ADAPTER.install(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"


def test_claude_no_hooks_keeps_permissions_and_mcp(tmp_path):
    claude_code.ADAPTER.install(tmp_path, hooks=False)
    data = load(tmp_path / ".claude" / "settings.json")
    assert "hooks" not in data
    assert "Bash(lawkeep approve *)" in data["permissions"]["ask"]


def test_user_edited_lawkeep_hook_is_not_replaced(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    custom = {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "lawkeep hook pre-tool-use"}],
    }
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [custom]}}))
    claude_code.ADAPTER.install(tmp_path)
    assert load(settings)["hooks"]["PreToolUse"] == [custom]


# --- Codex ------------------------------------------------------------------------------------


def test_codex_install_and_merge(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text(
        'model = "gpt-5"\n# keep this comment\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\n'
    )
    r = codex.ADAPTER.install(tmp_path)
    text = cfg.read_text()
    assert "# keep this comment" in text
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5"
    assert data["mcp_servers"]["docs"]["command"] == "docs-mcp"
    entry = data["mcp_servers"]["lawkeep"]
    assert entry["command"] == "sh" and "exec lawkeep mcp" in entry["args"][1]
    assert "/.venv/bin/lawkeep" in entry["args"][1]
    hooks = load(tmp_path / ".codex" / "hooks.json")["hooks"]
    pre = hooks["PreToolUse"][0]
    assert "apply_patch" in pre["matcher"] and "Bash" in pre["matcher"]
    assert "lawkeep hook pre-tool-use --agent codex" in pre["hooks"][0]["command"]
    assert "SessionStart" in hooks
    rules = (tmp_path / ".codex" / "rules" / "lawkeep.rules").read_text()
    assert 'pattern = ["lawkeep", "approve"]' in rules and 'decision = "prompt"' in rules
    assert ".codex/hooks.json" in r.files_changed
    again = codex.ADAPTER.install(tmp_path)
    assert again.files_changed == [] and cfg.read_text() == text


def test_codex_bad_toml_is_left_alone(tmp_path):
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("this is = = not toml")
    r = codex.ADAPTER.install(tmp_path)
    assert cfg.read_text() == "this is = = not toml"
    assert any("could not be read" in w for w in r.warnings)


# --- Cursor -----------------------------------------------------------------------------------


def test_cursor_install_and_merge(tmp_path):
    hooks_file = tmp_path / ".cursor" / "hooks.json"
    hooks_file.parent.mkdir()
    hooks_file.write_text(
        json.dumps({"version": 1, "hooks": {"afterFileEdit": [{"command": "./fmt.sh"}]}})
    )
    cursor.ADAPTER.install(tmp_path)
    data = load(hooks_file)
    assert data["hooks"]["afterFileEdit"] == [{"command": "./fmt.sh"}]
    assert "lawkeep hook pre-tool-use --agent cursor" in data["hooks"]["preToolUse"][0]["command"]
    assert "Write" in data["hooks"]["preToolUse"][0]["matcher"]
    assert "lawkeep hook pre-tool-use" in data["hooks"]["beforeShellExecution"][0]["command"]
    rule = (tmp_path / ".cursor" / "rules" / "lawkeep.mdc").read_text()
    assert rule.startswith("---\n") and "alwaysApply: true" in rule and "lawkeep propose" in rule
    cur = load(tmp_path / ".cursor" / "mcp.json")["mcpServers"]["lawkeep"]
    assert cur["command"] == "sh" and "/.venv/bin/lawkeep" in cur["args"][1]
    assert cursor.ADAPTER.install(tmp_path).files_changed == []


# --- Copilot ----------------------------------------------------------------------------------


def test_copilot_install_and_merge(tmp_path):
    instr = tmp_path / ".github" / "copilot-instructions.md"
    instr.parent.mkdir()
    instr.write_text("Prefer small functions.\n")
    copilot.ADAPTER.install(tmp_path)
    text = instr.read_text()
    assert text.startswith("Prefer small functions.\n") and "lawkeep check" in text
    data = load(tmp_path / ".github" / "hooks" / "lawkeep.json")
    assert data["version"] == 1
    entry = data["hooks"]["preToolUse"][0]
    assert entry["type"] == "command" and entry["timeoutSec"] == 30
    assert "lawkeep hook pre-tool-use --agent copilot" in entry["bash"]
    assert "exec lawkeep mcp" in load(tmp_path / ".mcp.json")["mcpServers"]["lawkeep"]["args"][1]
    assert copilot.ADAPTER.install(tmp_path).files_changed == []


def test_all_adapters_share_one_mcp_json(tmp_path):
    claude_code.ADAPTER.install(tmp_path)
    copilot.ADAPTER.install(tmp_path)
    assert list(load(tmp_path / ".mcp.json")["mcpServers"]) == ["lawkeep"]


def test_mcp_launcher_falls_back_to_project_venv(tmp_path):
    """The MCP entry starts lawkeep from <project>/.venv when it is not on PATH."""
    import subprocess

    from lawkeep.adapters import base as adapters_base

    fake = tmp_path / ".venv" / "bin" / "lawkeep"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\necho started-from-venv "$@"\n')
    fake.chmod(0o755)
    script = adapters_base.mcp_launch_script(str(tmp_path))
    out = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}
    )
    assert out.stdout.strip() == "started-from-venv mcp", out
