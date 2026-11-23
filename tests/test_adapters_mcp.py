"""Workstream E: the MCP server returns the same JSON as the CLI's --json output."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from lawkeep import cli, mcp_server
from lawkeep.errors import SetupError


class FakeRunner:
    def __init__(
        self, out: str = '{"kind": "check", "exit_code": 0}', code: int = 0, err: str = ""
    ):
        self.calls: list[list[str]] = []
        self.out, self.code, self.err = out, code, err

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        return self.code, self.out, self.err


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    assert cli.main(["init", "--root", str(root), "--agent", "none", "--json"]) == 0
    (root / "laws" / "refunds.py").write_text(
        "from lawkeep import law, given, st\n\n"
        '@law("A refund is never negative")\n'
        "@given(x=st.integers())\n"
        "def refund_never_negative(x):\n"
        "    assert abs(x) >= 0\n",
        encoding="utf-8",
    )
    (root / "laws" / "drafts" / "fees.py").write_text(
        'from lawkeep import law\n\n@law("Fees are at most 2%")\ndef fee_capped():\n    pass\n',
        encoding="utf-8",
    )
    return root


def test_check_runs_cli_with_json(tmp_path):
    runner = FakeRunner()
    tools = mcp_server.LawkeepTools(tmp_path, runner=runner)
    out = tools.check(profile="deep", laws=["refunds.a", "refunds.b"])
    assert json.loads(out) == {"kind": "check", "exit_code": 0}
    argv = runner.calls[0]
    assert argv[:4] == [sys.executable, "-m", "lawkeep", "check"]
    assert argv[4:] == [
        "--profile",
        "deep",
        "--law",
        "refunds.a",
        "--law",
        "refunds.b",
        "--json",
        "--root",
        str(tmp_path.resolve()),
    ]


def test_propose_and_report_arguments(tmp_path):
    runner = FakeRunner('{"kind": "propose", "exit_code": 2}')
    tools = mcp_server.LawkeepTools(tmp_path, runner=runner, actor="agent:claude-code")
    out = tools.propose("refunds.cap", "tiny refunds", file="laws/drafts/r.py")
    assert json.loads(out)["exit_code"] == 2
    assert runner.calls[-1][3:11] == [
        "propose",
        "refunds.cap",
        "--reason",
        "tiny refunds",
        "--actor",
        "agent:claude-code",
        "--file",
        "laws/drafts/r.py",
    ]
    tools.report()
    assert runner.calls[-1][3:5] == ["report", "--session"]
    tools.report(since="2026-11-21T10:00:00Z")
    assert runner.calls[-1][3:6] == ["report", "--since", "2026-11-21T10:00:00Z"]


def test_propose_with_content_writes_a_draft(tmp_path):
    root = make_project(tmp_path)
    runner = FakeRunner('{"kind": "propose", "exit_code": 0}')
    tools = mcp_server.LawkeepTools(root, runner=runner)
    tools.propose("refunds.refund_never_negative", "stricter", content="# new version\n")
    draft = root / "laws" / "drafts" / "proposed" / "refunds.refund_never_negative.py"
    assert draft.read_text() == "# new version\n"
    assert "laws/drafts/proposed/refunds.refund_never_negative.py" in runner.calls[-1]


def test_propose_needs_a_new_version(tmp_path):
    tools = mcp_server.LawkeepTools(tmp_path, runner=FakeRunner())
    data = json.loads(tools.propose("x", "why"))
    assert data["exit_code"] == 3 and "content" in data["summary"]


def test_non_json_cli_output_becomes_a_setup_problem(tmp_path):
    tools = mcp_server.LawkeepTools(
        tmp_path, runner=FakeRunner(out="Traceback ...", code=1, err="boom")
    )
    data = json.loads(tools.check())
    assert data["exit_code"] == 3 and data["kind"] == "check" and data["data"]["stderr"] == "boom"


def test_list_laws_reads_without_running_code(tmp_path):
    root = make_project(tmp_path)
    tools = mcp_server.LawkeepTools(root, runner=FakeRunner())
    data = json.loads(tools.list_laws())
    assert data["kind"] == "list_laws" and data["exit_code"] == 0
    ids = [law["law_id"] for law in data["data"]["laws"]]
    assert ids == ["refunds.refund_never_negative"]
    with_drafts = json.loads(tools.list_laws(include_drafts=True))
    assert len(with_drafts["data"]["laws"]) == 2


def test_check_end_to_end_matches_cli(tmp_path, capsys):
    """Real subprocess: the tool's JSON is the CLI's --json output (same kind/exit code)."""
    root = make_project(tmp_path)
    out = json.loads(mcp_server.LawkeepTools(root, timeout=300).check())
    capsys.readouterr()
    code = cli.main(["check", "--root", str(root), "--json"])
    direct = json.loads(capsys.readouterr().out)
    assert out["kind"] == direct["kind"] == "check"
    assert out["exit_code"] == direct["exit_code"] == code


def test_server_registers_four_tools(tmp_path):
    pytest.importorskip("mcp")
    # The real SDK server object; lawkeep's own type only names the methods it calls.
    server: Any = mcp_server.build_server(
        tmp_path, tools=mcp_server.LawkeepTools(tmp_path, runner=FakeRunner())
    )
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == ["check", "list_laws", "propose", "report"]
    result = asyncio.run(server.call_tool("check", {}))
    text = result.content[0].text if hasattr(result, "content") else result[0].text
    assert json.loads(text)["kind"] == "check"


def test_missing_mcp_package_is_setup_problem(monkeypatch, capsys):
    def missing():
        raise SetupError(
            "`lawkeep mcp` needs the optional `mcp` package, which is not installed.",
            hint=mcp_server.INSTALL_HINT,
        )

    monkeypatch.setattr(mcp_server, "_import_server_class", missing)
    code = cli.main(["mcp", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code == 3 and data["kind"] == "mcp"
    assert "lawkeep[mcp]" in data["hint"]


def test_import_server_class_without_mcp(monkeypatch):
    for name in ("mcp", "mcp.server", "mcp.server.mcpserver", "mcp.server.fastmcp"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(SetupError) as exc:
        mcp_server._import_server_class()
    assert "pip install 'lawkeep[mcp]'" in (exc.value.hint or "")
