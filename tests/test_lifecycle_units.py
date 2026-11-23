"""Unit tests for workstream D (law lifecycle): permissions, agent launcher, examples table
helpers, the law change report and drafting helpers. Made-up data only; no real agent."""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from lawkeep import agent_launcher, config, history, lawchanges
from lawkeep._probe_worker import readable
from lawkeep.commands import draft
from lawkeep.config import AgentConfig
from lawkeep.errors import ConfigError, SetupError
from lawkeep.examples_table import render_rows, select_varied
from lawkeep.models import ExampleRow, IntegrityIssue, ProtectionReport, ProtectionResult
from lawkeep.permissions import decide, permission_key

# --------------------------------------------------------------------------------- permissions


@pytest.mark.parametrize(
    "action,direction,key",
    [
        ("add_law", "stricter", "add_law"),
        ("change_law", "stricter", "add_law"),
        ("add_example", "stricter", "add_example"),
        ("add_example", None, "add_example"),
        ("rename_target", "same", "rename_target"),
        ("change_law", "same", "rename_target"),
        ("change_law", "unclear", "change_law"),
        ("change_law", None, "change_law"),
        ("change_law", "looser", "weaken_law"),
        ("add_example", "looser", "weaken_law"),
        ("remove_law", "stricter", "weaken_law"),
        ("skip_law", "same", "weaken_law"),
    ],
)
def test_permission_key_mapping(action, direction, key):
    assert permission_key(action, direction) == key


def test_decide_uses_config_and_defaults():
    cfg = config.default_config()
    d = decide(cfg, "add_law", "stricter")
    assert (d.key, d.value, d.may_apply) == ("add_law", "auto", True)
    d = decide(cfg, "change_law", "unclear")
    assert (d.key, d.value, d.may_apply) == ("change_law", "ask", False)
    assert "stricter or looser" in d.explanation
    cfg = config.loads('[permissions]\nadd_law = "ask"\nchange_law = "auto"\n')
    assert decide(cfg, "add_law", "stricter").may_apply is False
    assert decide(cfg, "change_law", "unclear").may_apply is True


def test_weaken_law_is_always_ask_even_if_config_is_tampered():
    cfg = config.default_config()
    cfg.permissions.weaken_law = "auto"  # bypassing config validation on purpose
    d = decide(cfg, "remove_law", None)
    assert d.key == "weaken_law" and d.value == "ask" and not d.may_apply
    assert "always needs your yes" in d.explanation
    assert decide(cfg, "change_law", "looser").may_apply is False


# ------------------------------------------------------------------------------ agent launcher


class FakeRunner:
    def __init__(self, returncode=0, stdout="done", stderr="", exc=None):
        self.calls = []
        self.returncode, self.stdout, self.stderr, self.exc = returncode, stdout, stderr, exc

    def __call__(self, argv, *, cwd, input, timeout, env):
        self.calls.append(
            {"argv": argv, "cwd": cwd, "input": input, "timeout": timeout, "env": env}
        )
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def test_launch_claude_preset_passes_prompt_as_argument(tmp_path):
    runner = FakeRunner()
    agent = AgentConfig(name="claude-code", timeout_seconds=30)
    res = agent_launcher.launch(agent, "write laws please", cwd=tmp_path, runner=runner)
    call = runner.calls[0]
    assert call["argv"][:4] == ["claude", "-p", "--permission-mode", "acceptEdits"]
    assert call["argv"][-1] == "write laws please"
    assert call["input"] is None and call["timeout"] == 30 and call["cwd"] == str(tmp_path)
    assert call["env"][agent_launcher.DRAFTING_ENV] == "1"
    assert res.ok and res.stdout == "done"
    assert "<prompt>" in res.argv  # the prompt itself is not echoed into reports


@pytest.mark.parametrize(
    "name,first",
    [
        ("codex", ["codex", "exec", "--sandbox", "workspace-write"]),
        ("cursor", ["cursor-agent", "-p", "--force"]),
        ("copilot", ["copilot", "-p"]),
    ],
)
def test_presets(name, first):
    argv, stdin = agent_launcher.build_argv(AgentConfig(name=name), "P")
    assert argv[: len(first)] == first and "P" in argv and stdin is None


def test_copilot_preset_cannot_run_shell_commands():
    argv, _ = agent_launcher.build_argv(AgentConfig(name="copilot"), "P")
    assert "--allow-all-tools" not in argv and "--allow-all" not in argv
    allow = argv[argv.index("--allow-tool") + 1]
    deny = argv[argv.index("--deny-tool") + 1]
    assert "shell" not in allow and allow.startswith("read, write(laws/drafts/*)")
    assert [k.strip() for k in deny.split(",")] == ["shell", "url"]
    assert "--no-ask-user" in argv


def test_custom_command_with_stdin_and_prompt_file(tmp_path):
    runner = FakeRunner()
    agent_launcher.launch(
        AgentConfig(command="my-agent --batch"), "hello", cwd=tmp_path, runner=runner
    )
    assert runner.calls[0]["argv"] == ["my-agent", "--batch"]
    assert runner.calls[0]["input"] == "hello"

    seen = {}

    def reading_runner(argv, *, cwd, input, timeout, env):
        seen["text"] = Path(argv[-1]).read_text(encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    agent_launcher.launch(
        AgentConfig(command="my-agent --file {prompt_file}"),
        "from file",
        cwd=tmp_path,
        runner=reading_runner,
    )
    assert seen["text"] == "from file"


def test_launch_errors_are_setup_problems(tmp_path):
    with pytest.raises(ConfigError):
        agent_launcher.launch(AgentConfig(), "x", cwd=tmp_path, runner=FakeRunner())
    with pytest.raises(SetupError, match="was not found"):
        agent_launcher.launch(
            AgentConfig(command="nope-agent {prompt}"),
            "x",
            cwd=tmp_path,
            runner=FakeRunner(exc=FileNotFoundError()),
        )
    with pytest.raises(SetupError, match="did not finish within 5 seconds"):
        agent_launcher.launch(
            AgentConfig(command="slow {prompt}"),
            "x",
            cwd=tmp_path,
            timeout=5,
            runner=FakeRunner(exc=subprocess.TimeoutExpired("slow", 5)),
        )
    with pytest.raises(ConfigError, match="too long"):
        agent_launcher.build_argv(AgentConfig(command="a {prompt}"), "x" * 200_000)


def test_launch_default_runner_with_a_fake_agent_script(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import sys\nprint('got', sys.stdin.read().upper())\n", encoding="utf-8")
    agent = AgentConfig(command=f"{sys.executable} {script}")
    res = agent_launcher.launch(agent, "hi there", cwd=tmp_path)
    assert res.returncode == 0 and "got HI THERE" in res.stdout


# ------------------------------------------------------------------------------ examples table


def test_readable_values():
    from dataclasses import dataclass

    @dataclass
    class Charge:
        total: Decimal
        currency: str

    assert readable(Decimal("0.50")) == "0.50"
    assert readable("tea") == "'tea'"
    assert readable(Charge(Decimal("4.00"), "EUR")) == "Charge(total=4.00, currency='EUR')"
    assert readable([1, 2]) == "[1, 2]" and readable((1,)) == "(1,)"
    assert readable(list(range(20))).endswith(", ...]")
    assert len(readable("x" * 500)) <= 120


def _case(inputs, outcome, source="generated", result=None):
    return {
        "inputs": inputs,
        "source": source,
        "real": {"law": {"outcome": outcome, "result": result}},
    }


def test_select_varied_keeps_failures_examples_and_edges():
    cases = [_case({"n": str(i)}, "holds") for i in range(1, 40)]
    cases.append(_case({"n": "0"}, "holds"))
    cases.append(_case({"n": "77"}, "fails"))
    cases.append(_case({"n": "500"}, "holds", source="explicit"))
    cases.append(_case({"n": "1"}, "holds"))  # duplicate input
    cases.append(_case({"n": "99"}, "skipped"))
    chosen = select_varied(cases, "law", 6)
    values = [c["inputs"]["n"] for c in chosen]
    assert len(values) == 6 and len(set(values)) == 6
    assert "77" in values and "500" in values and "0" in values
    assert "99" not in values


def test_render_rows_plain_and_proposal():
    rows = [
        ExampleRow(inputs={"amount": "40", "fee": "1"}, result="refund() → 40", holds=True),
        ExampleRow(inputs={}, result=None, holds=False, note="edge case: zero"),
    ]
    text = render_rows(rows)
    assert "amount=40, fee=1" in text and "yes ✓" in text and "NO ✗" in text
    assert "(no inputs)" in text and "law holds?" in text
    prop = render_rows([ExampleRow(inputs={"a": "1"}, result="x", holds=True, old_holds=False)])
    assert "old law holds?" in prop and "new law holds?" in prop
    assert "no examples" in render_rows([])


# ------------------------------------------------------------------------------- law changes


def _project(tmp_path: Path) -> Path:
    (tmp_path / "laws").mkdir()
    (tmp_path / "lawkeep.toml").write_text("", encoding="utf-8")
    (tmp_path / "laws" / "history.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "laws" / "failing_examples.jsonl").write_text("", encoding="utf-8")
    history.record_change(tmp_path, action="init", actor="person:ada", direction="stricter")
    return tmp_path


def test_render_since_shows_added_updated_waiting_rejected(tmp_path, monkeypatch):
    root = _project(tmp_path)
    start = history.mark_session_start(root, agent="claude-code")
    history.record_change(
        root,
        action="add_law",
        actor="agent:claude-code",
        law_id="R7",
        direction="stricter",
        permission_used="auto",
        sentence_after="A refund is never negative",
        details={"evidence": "catches 4 of 4 planted bugs"},
    )
    history.record_change(
        root,
        action="rename_target",
        actor="agent:claude-code",
        law_id="R3",
        direction="same",
        permission_used="auto",
        sentence_before="Refund is capped",
        sentence_after="Refund is capped",
        details={"evidence": "same meaning, confirmed"},
    )
    waiting = history.record_change(
        root,
        action="change_law",
        actor="agent:claude-code",
        law_id="R2",
        direction="looser",
        permission_used="ask",
        status="waiting",
        reason="no fees on tiny refunds",
        sentence_before="Fee is always 2%",
        sentence_after="Fee is 2%, or $0 for refunds under $1",
        details={
            "examples": [
                ExampleRow(
                    inputs={"refund": "0.50"}, result="fee() → 0.00", holds=True, old_holds=False
                ).to_dict()
            ]
        },
    ).entry
    decided = history.record_change(
        root,
        action="change_law",
        actor="agent:codex",
        law_id="R9",
        direction="looser",
        permission_used="ask",
        status="waiting",
        reason="tests are slow",
    ).entry
    history.record_change(
        root,
        action="change_law",
        actor="person:ada",
        law_id="R9",
        direction="looser",
        permission_used="ask",
        status="rejected",
        refers_to=decided.id,
    )
    monkeypatch.setenv("COLUMNS", "200")
    text = lawchanges.render_since(root, start, integrity=[])
    assert text.splitlines()[0] == "Law changes in this session"
    assert "Added" in text and "“A refund is never negative”" in text
    assert "went in without asking (your settings allow it)" in text
    assert "catches 4 of 4 planted bugs" in text
    assert "Changed" in text and "same meaning" in text
    assert "“Fee is always 2%” → “Fee is 2%, or $0 for refunds under $1”" in text
    assert "Waiting for you" in text and "Looser: needs your yes" in text
    assert "why: no fees on tiny refunds" in text
    assert "now allowed: refund=0.50 → fee() → 0.00" in text
    assert "Rejected" in text and "rejected by ada" in text
    assert text.count("Rejected") == 1  # the decided waiting entry is shown once, as its decision
    assert "1 item(s) need a person's decision" in text
    assert "set up" not in text  # init was before the session

    changes = lawchanges.changes_since(root, start)
    assert [c.change for c in changes] == ["added", "updated", "waiting", "rejected"]
    assert changes[2].examples[0].inputs == {"refund": "0.50"}
    pending = lawchanges.list_pending(root)
    assert [p.history_id for p in pending] == [waiting.id]

    md = lawchanges.render_since(root, None, integrity=[], as_markdown=True)
    assert md.startswith("### All recorded law changes")
    assert "| Added | “A refund is never negative”<br>`R7` |" in md
    assert "lawkeep was set up" in md and "| **Waiting for you** |" in md
    assert "\x1b[" not in md


def test_render_since_integrity_and_protection(tmp_path, monkeypatch):
    root = _project(tmp_path)
    issue = IntegrityIssue(
        kind="unrecorded_law_change",
        message="laws/fees.py changed outside lawkeep",
        path="laws/fees.py",
    )
    prot = ProtectionReport(
        functions=[
            ProtectionResult(
                function="shop:refund",
                file="src/shop.py",
                status="protected",
                planted_bugs_total=3,
                planted_bugs_caught=3,
            ),
            ProtectionResult(
                function="shop:fee",
                file="src/shop.py",
                status="not_protected",
                planted_bugs_total=2,
                planted_bugs_caught=0,
            ),
        ]
    )
    monkeypatch.setenv("COLUMNS", "200")
    text = lawchanges.render_since(root, "2999-01-01T00:00:00Z", integrity=[issue], protection=prot)
    assert "! laws/fees.py changed outside lawkeep" in text
    assert "1 function(s) protected by laws, 0 partly, 1 not protected" in text
    assert "shop:fee (src/shop.py): not protected, 0 of 2 planted bugs caught" in text
    assert "1 item(s) need a person's decision" in text
    report = lawchanges.build_report(root, None, integrity=[issue])
    assert report.exit_code == 2 and report.kind == "report"
    assert report.to_dict()["changes"][0]["change"] == "added"


def test_render_since_without_integrity_degrades(tmp_path, monkeypatch):
    root = _project(tmp_path)
    import lawkeep.integrity as integ

    def boom(*a, **k):
        raise NotImplementedError

    monkeypatch.setattr(integ, "check_integrity", boom)
    text = lawchanges.render_since(root, None)
    assert "not available yet" in text


# ------------------------------------------------------------------------------ draft helpers


def test_public_api_shows_signatures_not_bodies(tmp_path):
    src = tmp_path / "shop.py"
    src.write_text(
        '"""Shop."""\nRATE = 2\n\n'
        "def refund(charge: int, amount: int) -> int:\n"
        '    """Refund up to the charge."""\n    return min(charge, amount)  # SECRET_BODY\n\n'
        "def _hidden():\n    pass\n\n"
        "class Till:\n    total: int = 0\n    def add(self, n: int) -> None:\n"
        '        """Add n."""\n        self.total += n\n',
        encoding="utf-8",
    )
    api = draft.public_api(src, "shop")
    assert "def refund(charge: int, amount: int) -> int:" in api
    assert "Refund up to the charge." in api and "RATE = 2" in api
    assert (
        "class Till:" in api and "total: int = 0" in api and "def add(self, n: int) -> None:" in api
    )
    assert "SECRET_BODY" not in api and "min(charge" not in api and "_hidden" not in api


def test_build_prompt_contains_request_rules_and_cookbook():
    prompt = draft.build_prompt(
        request="refunds never exceed the payment",
        import_name="shop.refunds",
        api="def refund(a, b): ...",
        docs=[("SPEC.md", "Refund rules")],
        existing=[("refunds.capped", "A refund is capped")],
        has_strategies=True,
    )
    for text in (
        "refunds never exceed the payment",
        "laws/drafts/refunds_<short_name>.py",
        "Round trip",
        "Idempotence",
        "Conservation",
        "Exact amount",
        "SPEC.md",
        "refunds.capped",
        "Do NOT edit",
        "laws/strategies.py",
        "from shop.refunds import",
    ):
        assert text in prompt


def test_strategies_change_detects_additions_and_edits():
    old = "def a():\n    return 1\n"
    assert draft.strategies_change(old, old + "\ndef b():\n    return 2\n") == (True, ["b"], [])
    assert draft.strategies_change(old, "def a():\n    return 9\n") == (False, [], ["a"])
    assert draft.strategies_change(old, "")[2] == ["a"]
    assert draft.strategies_change("", "def b():\n    pass\n") == (True, ["b"], [])
