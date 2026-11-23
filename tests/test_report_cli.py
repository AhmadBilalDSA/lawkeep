"""`lawkeep report` through the CLI, on a made-up project in a throwaway git repository."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

from lawkeep import cli, history

FIXTURE = Path(__file__).parent / "fixtures" / "strength" / "shop"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Person",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Person",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(root: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV, "HOME": str(root.parent)}
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def make_project(tmp_path: Path) -> Path:
    """The shop fixture, set up like `lawkeep init` would (lock + history), committed on main,
    then a feature branch that adds an unprotected function."""
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    (root / "laws" / "history.jsonl").write_text("")
    (root / "laws" / "failing_examples.jsonl").write_text("")
    (root / "laws" / "strategies.py").write_text('"""Project input generators."""\n')
    history.record_change(root, action="init", actor="person:Test", direction="stricter")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feature")
    (root / "src" / "shop" / "discounts.py").write_text(
        textwrap.dedent(
            """\
            def discount(total: int) -> int:
                if total >= 100:
                    return total // 20
                return 0
            """
        )
    )
    return root


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


def test_report_json_has_protection_and_changes(tmp_path, capsys):
    root = make_project(tmp_path)
    code, out = run_cli(capsys, "report", "--json", "--root", str(root))
    data = json.loads(out)
    assert data["kind"] == "report" and data["exit_code"] == code
    prot = data["protection"]
    assert prot["kind"] == "protection" and prot["base_ref"] == "main"
    statuses = {f["function"]: f["status"] for f in prot["functions"]}
    assert statuses == {"shop.discounts:discount": "not_protected"}
    assert any(c["history_id"] for c in data["changes"])  # the init entry
    assert "not fully protected" in data["summary"]
    issues = [i for i in data["integrity"] if i["severity"] == "error"]
    assert code == (2 if issues else 0), issues


def test_report_waiting_decision_returns_2_and_markdown(tmp_path, capsys):
    root = make_project(tmp_path)
    history.record_change(
        root,
        action="change_law",
        actor="agent:claude-code",
        law_id="refunds.refund_is_capped",
        direction="looser",
        reason="no fees on tiny refunds",
        permission_used="ask",
        status="waiting",
    )
    code, out = run_cli(capsys, "report", "--root", str(root), "--format", "markdown")
    assert code == 2
    assert out.startswith("## lawkeep report")
    assert "| `shop.discounts:discount` | **Not protected — read this one** |" in out
    assert "**WAITING FOR YOUR DECISION**" in out and "**Next step:**" in out
    assert "\x1b[" not in out  # Markdown never carries terminal colours
    assert "refunds.refund_is_capped" in out
    assert "does not block merges" in out


def test_report_text_and_no_protection(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("COLUMNS", "200")
    code, out = run_cli(capsys, "report", "--root", str(root))
    assert "Is the code you changed since main protected by laws?" in out
    assert "Not protected — read this one" in out and "discount()" in out
    assert "Next step:" in out
    code, out = run_cli(capsys, "report", "--json", "--root", str(root), "--no-protection")
    assert json.loads(out)["protection"] is None


def test_report_since_filters_and_bad_since_is_setup_problem(tmp_path, capsys):
    root = make_project(tmp_path)
    code, out = run_cli(
        capsys,
        "report",
        "--json",
        "--root",
        str(root),
        "--no-protection",
        "--since",
        "2999-01-01T00:00:00Z",
    )
    assert json.loads(out)["changes"] == []
    code, out = run_cli(capsys, "report", "--json", "--root", str(root), "--since", "yesterday-ish")
    assert code == 3
    assert "yesterday-ish" in json.loads(out)["summary"] + json.dumps(json.loads(out))


def test_report_unknown_base_is_setup_problem(tmp_path, capsys):
    root = make_project(tmp_path)
    code, out = run_cli(capsys, "report", "--json", "--root", str(root), "--base", "nope")
    assert code == 3


def test_report_markdown_unrecorded_change_renders_cleanly(tmp_path, capsys):
    """An unrecorded law change in the PR comment: one label, no broken code spans."""
    root = make_project(tmp_path)
    law = next((root / "laws").glob("[!_]*.py"))
    law.write_text(law.read_text() + "\n# edited by hand\n")
    code, out = run_cli(
        capsys, "report", "--root", str(root), "--no-protection", "--format", "markdown"
    )
    assert code == 2
    line = next(x for x in out.splitlines() if "unrecorded law change" in x)
    assert line.count("unrecorded law change") == 1, line
    assert "`" not in line, line
    assert "lawkeep approve" in line


def test_report_no_git(tmp_path, capsys):
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    code, out = run_cli(capsys, "report", "--json", "--root", str(root))
    data = json.loads(out)
    assert data["protection"]["base_ref"] is None
    assert any("not a git repository" in n for n in data["protection"]["notes"])
