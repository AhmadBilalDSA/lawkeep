"""Long runs without a person (2026-11-23): "park and keep going" and "one waiting list".

- every agent instruction source carries the rules for when nobody is there;
- `presence.detect` (the `person_present` guess) follows its documented rules;
- the waiting count shows in `check`, `report` (text and Markdown) and the session hooks;
- `lawkeep approve` lists all five kinds in one place (JSON is additive);
- "approve all recommended" never takes anything looser, unclear, unrecorded, unconfirmed or
  made by the drafting AI;
- approvals made without a terminal can be confirmed or undone by a person, and both are
  recorded.

Made-up tea shop project (tests/fixtures/lifecycle/), a fake drafting agent, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from lawkeep import approvals, cli, direction, history, lock, presence, waiting
from lawkeep.adapters import base, cursor
from lawkeep.commands import approve as approve_cmd
from lawkeep.commands import draft as draft_cmd
from lawkeep.commands import hook as hook_cmd
from lawkeep.mcp_server import INSTRUCTIONS
from lawkeep.models import QuickCheckOutcome, StrengthResult

FIX = Path(__file__).parent / "fixtures" / "lifecycle"
LAW_ID = "pricing.discount_never_raises_price"


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch):
    monkeypatch.setenv(direction.PLANTED_BUGS_ENV, "0")
    for var in (
        "CI",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ATTENDED",
        presence.ENV,
    ):
        monkeypatch.delenv(var, raising=False)


def make_project(tmp_path: Path, extra_toml: str = "", quick_check: bool = False) -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    agent = f"{sys.executable} {FIX / 'fake_agent.py'} {{prompt_file}}"
    qc = "true" if quick_check else "false"
    (root / "lawkeep.toml").write_text(
        f'[agent]\nname = "other"\ncommand = "{agent}"\n\n[strength]\nquick_check = {qc}\n'
        + extra_toml,
        encoding="utf-8",
    )
    for name in ("history.jsonl", "failing_examples.jsonl"):
        (root / "laws" / name).write_text("", encoding="utf-8")
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def lk(capsys, root: Path, *args: str, as_json: bool = True):
    argv = [*args, "--root", str(root)] + (["--json"] if as_json else [])
    old = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "200"
    try:
        code = cli.main(argv)
    finally:
        if old is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = old
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


def propose(capsys, root: Path, name: str):
    return lk(
        capsys,
        root,
        "propose",
        LAW_ID,
        "--file",
        str(FIX / "proposals" / f"{name}.py"),
        "--reason",
        f"because {name}",
        "--actor",
        "agent:codex",
    )


def fake_strong_quick_check(monkeypatch) -> None:
    import lawkeep.strength as strength

    def fake_quick(root_, cfg, *, law_ids=None, include_drafts=False):
        return [
            StrengthResult(
                law_id=i,
                quick_check=[
                    QuickCheckOutcome("teashop.pricing:total", "always_zero", True),
                    QuickCheckOutcome("teashop.pricing:total", "raises", True),
                ],
            )
            for i in law_ids or []
        ]

    monkeypatch.setattr(strength, "run_quick_check", fake_quick)


# ---------------------------------------------------------------------- agent instructions

KEY_PHRASES = (
    "propose",  # park it
    "never run `lawkeep approve`",
    "keep going",
    "does not depend on",
    "summary",
    "waiting for the person",
)


@pytest.mark.parametrize(
    "name",
    ["agents_snippet", "session_start", "mcp", "cursor_rule", "draft_prompt"],
)
def test_every_instruction_source_has_the_no_person_rules(name):
    if name == "agents_snippet":
        text = base.agents_snippet()
        assert "When no person is there" in text
        assert "stop and ask the person** (when they are there)" in text  # interactive rule
    elif name == "session_start":
        text = f"{hook_cmd.REMINDER} {hook_cmd.NO_PERSON}"
        assert "stop and ask the person" in text
    elif name == "mcp":
        text = INSTRUCTIONS
        assert "person is there, stop and ask them" in text
        text = text.replace("never approve anything", "never run `lawkeep approve`")
    elif name == "cursor_rule":
        text = cursor.rule_text()  # Copilot gets the same snippet (base.agents_snippet)
    else:
        text = draft_cmd.build_prompt(
            request="",
            import_name="teashop.pricing",
            api="",
            docs=[],
            existing=[],
            has_strategies=False,
        )
        assert "Nobody is there to answer questions" in text
        assert "Do not run `lawkeep approve`" in text
        return
    low = " ".join(text.lower().split())
    for phrase in KEY_PHRASES:
        assert phrase.lower() in low, (name, phrase)
    assert "person_present" in text


# ---------------------------------------------------------------------- person_present


@pytest.mark.parametrize(
    ("env", "tty", "present"),
    [
        ({presence.ENV: "0"}, True, False),  # explicit answer wins, even in a terminal
        ({presence.ENV: "yes", "CI": "true"}, False, True),
        ({"CI": "true"}, True, False),  # CI: nobody can answer
        ({}, True, True),  # typed in a terminal
        ({"CLAUDE_CODE_SESSION_ATTENDED": "1"}, False, True),
        ({"CLAUDE_CODE_SESSION_ATTENDED": "0", "CLAUDE_CODE_ENTRYPOINT": "cli"}, False, False),
        ({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, False, True),
        ({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, False, False),
        ({}, False, False),  # Codex, Cursor, Copilot, scripts: nobody known to be there
    ],
)
def test_person_present_heuristic(env, tty, present):
    seen = presence.detect(env, stdin_tty=tty)
    assert seen.present is present, seen
    assert seen.reason and seen.to_dict()["person_present"] is present


# ---------------------------------------------------------------------- park and keep going


def test_propose_without_a_person_says_park_and_keep_going(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv(presence.ENV, "0")
    code, data = propose(capsys, root, "looser")
    assert code == 2 and data["decision"] == "waiting"
    hid = data["history_id"]
    assert data["person_present"] is False
    assert data["next_step"].startswith(f"Parked as {hid}. Continue with work that does not")
    assert f"list {hid} in your final summary" in data["next_step"]
    # a person present: today's "show this to the person"
    monkeypatch.setenv(presence.ENV, "1")
    code, text = lk(
        capsys,
        root,
        "propose",
        LAW_ID,
        "--file",
        str(FIX / "proposals" / "skip.py"),
        "--reason",
        "x",
        as_json=False,
    )
    assert code == 2 and "Next step: Show this to the person" in text


# ---------------------------------------------------------------------- the waiting count


def _draft_and_proposal(tmp_path, capsys, monkeypatch) -> Path:
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "good")
    lk(capsys, root, "draft", "teashop.pricing")
    code, data = propose(capsys, root, "looser")
    assert code == 2, data
    return root


LINE = (
    "2 things wait for your decision (1 new draft, 1 proposed law change) — run `lawkeep approve`."
)


def test_waiting_count_in_check_report_hooks_and_markdown(tmp_path, capsys, monkeypatch):
    root = _draft_and_proposal(tmp_path, capsys, monkeypatch)

    code, data = lk(capsys, root, "check")
    assert code == 2  # exit code 2 keeps its meaning: a proposal waits
    w = data["waiting_for_person"]
    assert w["count"] == 2 and w["by_kind"]["draft"] == 1 and w["by_kind"]["proposal"] == 1
    assert "laws/drafts/pricing_total.py" in w["ids"]
    assert data["person_present"] is False and data["person_present_reason"]
    code, text = lk(capsys, root, "check", as_json=False)
    assert text.count(LINE) == 1

    code, text = lk(capsys, root, "report", "--no-protection", as_json=False)
    assert LINE in text
    code, md = lk(capsys, root, "report", "--no-protection", "--format", "markdown", as_json=False)
    assert f"**{LINE}**" in md
    code, data = lk(capsys, root, "report", "--no-protection")
    assert data["waiting_for_person"]["count"] == 2 and "person_present" in data

    monkeypatch.setattr(hook_cmd, "read_payload", lambda stream=None: {"cwd": str(root)})
    assert cli.main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert LINE in out and "When" not in out[:0]  # the count line, as is
    assert "lawkeep's guess: a person is probably not there" in out
    assert cli.main(["hook", "session-end"]) == 0
    assert LINE in " ".join(capsys.readouterr().out.split())
    assert cli.main(["hook", "session-start", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["data"]["waiting_for_person"]["count"] == 2
    assert data["data"]["person_present"] is False


def test_nothing_waiting_shows_no_count(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = lk(capsys, root, "check")
    assert code == 0 and data["waiting_for_person"]["count"] == 0
    code, text = lk(capsys, root, "check", as_json=False)
    assert "wait for your decision" not in text and "waits for your decision" not in text
    assert waiting.collect(root).line() is None


# ---------------------------------------------------------------------- one waiting list


def _all_five_kinds(tmp_path, capsys, monkeypatch) -> tuple[Path, dict[str, str]]:
    root = make_project(tmp_path)
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: False)
    # 1. a draft + 2. a change the drafting AI made to the approved law (parked as draft_edit)
    monkeypatch.setenv("FAKE_AGENT_MODE", "edit_looser")
    lk(capsys, root, "draft", "teashop.pricing")
    # 3. a looser proposal
    code, data = propose(capsys, root, "skip")
    assert code == 2, data
    proposal = data["history_id"]
    # 4. an approval recorded without a terminal (an agent ran approve --yes --by)
    rec = history.record_change(
        root,
        action="change_settings",
        actor="agent:codex",
        approved_by="person:Ada",
        permission_used="ask",
        reason="more examples",
        details={"terminal": False, "channel": "agent permission prompt"},
    )
    # 5. a law file changed outside lawkeep (a comment added to lawkeep.toml by hand)
    toml = root / "lawkeep.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "# edited by hand\n", encoding="utf-8")
    return root, {"proposal": proposal, "unconfirmed": rec.entry.id}


def test_one_waiting_list_shows_all_five_kinds(tmp_path, capsys, monkeypatch):
    root, ids = _all_five_kinds(tmp_path, capsys, monkeypatch)
    code, data = lk(capsys, root, "approve")
    assert code == 2, data
    groups = [w["group"] for w in data["data"]["waiting"]]
    assert sorted(groups) == sorted(waiting.KINDS), groups
    summary = data["data"]["waiting_for_person"]
    assert summary["count"] == 5 and set(summary["by_kind"].values()) == {1}
    assert ids["proposal"] in summary["ids"] and ids["unconfirmed"] in summary["ids"]
    assert not data["data"]["decided"]  # nothing is decided without a person
    assert all(not w["batch_safe"] for w in data["data"]["waiting"])
    unconf = next(w for w in data["data"]["waiting"] if w["group"] == "unconfirmed")
    assert unconf["approval"]["history_ids"] == [ids["unconfirmed"]]
    # the same five in check's count (static, no code runs for the list)
    wl = waiting.collect(root)
    assert wl.count == 5 and wl.counts() == {k: 1 for k in waiting.KINDS}

    code, text = lk(capsys, root, "approve", as_json=False)
    assert code == 2
    assert "5 things wait for your decision" in text
    for label in (
        "1 new draft",
        "1 proposed law change",
        "1 change by the drafting AI",
        "1 law change made outside lawkeep",
        "1 approval made without a terminal",
    ):
        assert label in text, label
    assert "Approval made without a terminal" in text and "Confirm:" in text


def test_yes_without_ids_never_decides_the_risky_kinds(tmp_path, capsys, monkeypatch):
    root, ids = _all_five_kinds(tmp_path, capsys, monkeypatch)
    toml = root / "lawkeep.toml"
    toml.write_text(toml.read_text(encoding="utf-8").replace("# edited by hand\n", ""))
    code, data = lk(capsys, root, "approve", "--yes", "--by", "Ada", "--channel", "prompt")
    decided = {d["group"] for d in data["data"]["decided"]}
    assert decided <= {"draft"}, decided
    left = waiting.collect(root).counts()
    assert left["proposal"] == 1 and left["draft_edit"] == 1
    # an unconfirmed approval is never confirmed or undone without a terminal
    code, data = lk(capsys, root, "approve", ids["unconfirmed"], "--yes", "--by", "Ada")
    assert code == 3 and "in a terminal" in " ".join(data["data"]["problems"])


def test_batch_option_only_takes_safe_items(tmp_path, capsys, monkeypatch):
    root, ids = _all_five_kinds(tmp_path, capsys, monkeypatch)
    # make the draft measurably strong, and add a stricter change that waits for a yes
    toml = root / "lawkeep.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("quick_check = false", "quick_check = true")
        + '\n[permissions]\nadd_law = "ask"\n'
    )
    fake_strong_quick_check(monkeypatch)
    lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    code, data = propose(capsys, root, "stricter")
    assert code == 2 and data["direction"] == "stricter", data
    stricter = data["history_id"]

    # a person in a terminal: accept nothing else, but say yes to the batch
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: True)
    asked: list[str] = []

    def answer(q: str) -> bool:
        asked.append(q)
        return q.startswith("Approve all")

    monkeypatch.setattr(approve_cmd, "_ask", answer)
    monkeypatch.setattr(approve_cmd, "_choose", lambda q, options: "skip")
    code, text = lk(capsys, root, "approve", "--by", "Ada", as_json=False)
    assert "Approve all 2 recommended new drafts and stricter changes?" in asked
    in_batch = [
        e
        for e in history.read(root)
        if e.details.get("channel") == "terminal (approve all recommended)"
    ]
    assert {e.refers_to or e.details.get("draft_file") for e in in_batch} == {
        stricter,
        "laws/drafts/pricing_total.py",
    }
    risky = {"looser", "unclear"}
    assert not any(e.direction in risky or e.details.get("from_draft") for e in in_batch)
    # the risky ones were asked one by one and (answered no) rejected; the unconfirmed skipped
    assert asked.count("Approve this change?") == 2
    assert history.find(history.read(root), ids["proposal"]) is not None
    left = waiting.collect(root).counts()
    # skipped: the settings approval, and the --accept-unrecorded made above without a terminal
    assert left == {**{k: 0 for k in waiting.KINDS}, "unconfirmed": 2}, left


def test_batch_safety_rules_per_kind(tmp_path):
    from lawkeep.models import HistoryEntry

    def proposal(**kw) -> approve_cmd.Item:
        e = HistoryEntry(action=kw.pop("action", "change_law"), actor="agent:x", **kw)
        e.details.setdefault("proposal_path", "laws/drafts/proposals/x/")
        return approve_cmd.Item("proposal", "h-1", "t", entry=e)

    assert approve_cmd._is_batch_safe(proposal(direction="stricter"), [])
    assert approve_cmd._is_batch_safe(proposal(direction="same"), [])
    for risky in (
        proposal(direction="looser"),
        proposal(direction="unclear"),
        proposal(direction="stricter", action="remove_law"),
        proposal(direction="stricter", action="skip_law"),
        proposal(direction="stricter", details={"from_draft": True}),
        proposal(direction="stricter", action="add_law", details={"strength": {"weak": 1}}),
        approve_cmd.Item("unrecorded", "unrecorded", "t", paths=["laws/x.py"]),
        approve_cmd.Item(
            "unconfirmed",
            "h-2",
            "t",
            group=approvals.UnconfirmedGroup(
                [HistoryEntry(action="add_law", actor="agent:x", approved_by="person:A")]
            ),
        ),
        approve_cmd.Item("draft", "laws/drafts/x.py", "t"),  # no laws in it
    ):
        assert not approve_cmd._is_batch_safe(risky, []), risky


# ---------------------------------------------------------------------- confirm / undo


def _approved_without_terminal(tmp_path, capsys, monkeypatch) -> tuple[Path, str, bytes]:
    root = make_project(tmp_path)
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: False)
    law = root / "laws" / "pricing.py"
    original = law.read_bytes()
    code, data = propose(capsys, root, "looser")
    hid = data["history_id"]
    code, data = lk(capsys, root, "approve", hid, "--yes", "--by", "Ada", "--channel", "prompt")
    assert code == 0, data
    assert law.read_bytes() != original
    applied = history.read(root)[-1]
    assert applied.details["terminal"] is False
    assert (approvals.undo_root(root) / applied.id / approvals.MANIFEST).is_file()
    return root, applied.id, original


def test_undo_an_unconfirmed_approval(tmp_path, capsys, monkeypatch):
    root, aid, original = _approved_without_terminal(tmp_path, capsys, monkeypatch)
    assert [w.kind for w in waiting.collect(root).items] == ["unconfirmed"]
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(approve_cmd, "_choose", lambda q, o: asked.append(q) or "undo")
    code, text = lk(capsys, root, "approve", "--by", "Ada", as_json=False)
    assert code == 0, text
    assert asked == ["Confirm or undo this approval?"]
    assert "Undone. The law files are back" in text
    assert (root / "laws" / "pricing.py").read_bytes() == original
    e = history.read(root)[-1]
    assert (e.action, e.status, e.refers_to) == ("undo_approval", "applied", aid)
    assert e.details["entries"] == [aid] and e.details["terminal"] is True
    assert e.approved_by == "person:Ada" and e.direction == "stricter"
    assert lock.diff_against_lock(root).is_empty
    assert waiting.collect(root).count == 0
    assert not (approvals.undo_root(root) / aid).exists()
    code, text = lk(capsys, root, "report", "--no-protection", "--all", as_json=False)
    assert "Approval undone" in text


def test_confirm_an_unconfirmed_approval(tmp_path, capsys, monkeypatch):
    root, aid, original = _approved_without_terminal(tmp_path, capsys, monkeypatch)
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: True)
    code, data = lk(capsys, root, "approve", aid, "--yes", "--by", "Ada")
    assert code == 0, data
    assert data["data"]["decided"][0]["decision"] == "confirmed"
    e = history.read(root)[-1]
    assert (e.action, e.details["entries"], e.details["terminal"]) == (
        "confirm_approval",
        [aid],
        True,
    )
    assert (root / "laws" / "pricing.py").read_bytes() != original  # nothing undone
    assert waiting.collect(root).count == 0
    assert lock.diff_against_lock(root).is_empty


def test_undo_a_draft_approval_puts_the_draft_back(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: False)
    monkeypatch.setenv("FAKE_AGENT_MODE", "good")
    lk(capsys, root, "draft", "teashop.pricing")
    code, data = lk(capsys, root, "approve", "laws/drafts/pricing_total.py", "--yes", "--by", "A")
    assert code == 0 and (root / "laws" / "pricing_total.py").is_file()
    aid = history.read(root)[-1].id
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: True)
    code, data = lk(capsys, root, "approve", aid, "--no", "--by", "Ada")
    assert code == 0, data
    assert not (root / "laws" / "pricing_total.py").exists()
    assert (root / "laws" / "drafts" / "pricing_total.py").is_file()
    assert lock.diff_against_lock(root).is_empty
    assert [w.kind for w in waiting.collect(root).items] == ["draft"]


def test_undo_refuses_when_the_file_changed_since(tmp_path, capsys, monkeypatch):
    root, aid, _ = _approved_without_terminal(tmp_path, capsys, monkeypatch)
    code, data = propose(capsys, root, "stricter")  # applied right away: the file moved on
    assert data["decision"] == "applied", data
    groups = approvals.unconfirmed_groups(history.read(root))
    ok, message = approvals.undo(root, groups[0], person="person:Ada", channel="terminal")
    assert not ok and "changed after this approval" in message
