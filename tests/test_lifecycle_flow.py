"""End-to-end tests for workstream D: draft (with a FAKE agent script), approve, propose,
direction and example tables, on the made-up tea shop in tests/fixtures/lifecycle/.

No real coding agent and no network: the agent is tests/fixtures/lifecycle/fake_agent.py.
Workstream C's quick check and planted bugs are switched off (or faked) so these tests only
exercise D's code.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from lawkeep import cli, config, direction, examples_table, history, lawchanges, lock
from lawkeep.commands import approve as approve_cmd
from lawkeep.models import QuickCheckOutcome, StrengthResult

FIX = Path(__file__).parent / "fixtures" / "lifecycle"
LAW_ID = "pricing.discount_never_raises_price"


@pytest.fixture(autouse=True)
def _no_planted_bugs(monkeypatch):
    monkeypatch.setenv(direction.PLANTED_BUGS_ENV, "0")


def make_project(tmp_path: Path, extra_toml: str = "") -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    agent = f"{sys.executable} {FIX / 'fake_agent.py'} {{prompt_file}}"
    (root / "lawkeep.toml").write_text(
        f'[agent]\nname = "other"\ncommand = "{agent}"\n\n[strength]\nquick_check = false\n'
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
    os.environ["COLUMNS"] = "200"  # human text unwrapped; narrow widths: test_ui_output.py
    try:
        code = cli.main(argv)
    finally:
        if old is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = old
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


def last(root: Path):
    return history.read(root)[-1]


def clean(root: Path) -> bool:
    return lock.diff_against_lock(root).is_empty


# ------------------------------------------------------------------------------------- draft


def test_draft_with_fake_agent_then_approve(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "good")
    code, data = lk(
        capsys, root, "draft", "teashop.pricing", "--request", "totals are never negative"
    )
    assert code == 2, data
    drafts = data["data"]["drafts"]
    assert [d["law_id"] for d in drafts] == ["pricing_total.total_never_negative"]
    assert drafts[0]["probe"]["status"] == "holds"
    assert drafts[0]["probe"]["rows"] and drafts[0]["probe"]["rows"][0]["result"].startswith(
        "total() → "
    )

    prompt = (root / "fake_agent_prompt.txt").read_text(encoding="utf-8")
    assert "totals are never negative" in prompt
    assert "def total(price: Decimal, quantity: int) -> Decimal:" in prompt
    assert "_internal_helper" not in prompt and "amount * percent / 100" not in prompt
    assert "A discount never makes the price larger" in prompt  # existing laws listed

    # the generator the agent ADDED was recorded automatically; the lock stays consistent
    strat_entry = [e for e in history.read(root) if e.action == "add_strategy"]
    assert strat_entry and strat_entry[0].permission_used == "auto"
    assert "cups" in (root / "laws" / "strategies.py").read_text(encoding="utf-8")
    assert clean(root)

    # with --json and no --yes: nothing is decided, the draft is listed with examples
    code, data = lk(capsys, root, "approve")
    assert code == 2
    waiting = data["data"]["waiting"]
    assert waiting[0]["kind"] == "draft" and waiting[0]["laws"][0]["probe"]["rows"]

    code, data = lk(
        capsys,
        root,
        "approve",
        "pricing_total.total_never_negative",
        "--yes",
        "--by",
        "Ada",
        "--channel",
        "agent permission prompt",
    )
    assert code == 0, data
    assert (root / "laws" / "pricing_total.py").is_file()
    assert not (root / "laws" / "drafts" / "pricing_total.py").exists()
    e = last(root)
    assert (e.action, e.status, e.approved_by, e.permission_used) == (
        "add_law",
        "applied",
        "person:Ada",
        "ask",
    )
    assert e.details["channel"] == "agent permission prompt"
    assert e.actor == "agent:other" and e.reason == "totals are never negative"
    assert "Works with your current code" in e.details["evidence"]
    assert clean(root)
    locked = lock.read_lock(root)
    assert locked is not None and "pricing_total.total_never_negative" in locked.laws

    monkeypatch.setenv("COLUMNS", "200")
    text = lawchanges.render_since(root, None, integrity=[])
    assert "Added" in text and "“The total price is never negative”" in text
    assert "approved by Ada" in text


def test_draft_failing_on_todays_code_is_shown(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "failing")
    code, text = lk(capsys, root, "draft", "src/teashop/pricing.py", as_json=False)
    assert code == 1
    assert (
        "Breaks with your current code — lawkeep found an input where the code does not follow "
        "this law. Either the code has a bug or the law is wrong: you decide." in text
    )
    assert "The input that breaks it: price: $5" in text and "quantity: 3" in text
    assert "total() gave 15" in text
    assert " NO " in text and "Decide carefully" in text


def test_draft_keeps_agent_to_drafts(tmp_path, capsys, monkeypatch):
    """The agent's edits to an approved law and an existing generator are put back and wait
    as proposals; the generator it only ADDED is recorded as before."""
    root = make_project(tmp_path)
    law_before = (root / "laws" / "pricing.py").read_bytes()
    monkeypatch.setenv("FAKE_AGENT_MODE", "naughty")
    code, data = lk(capsys, root, "draft", "teashop.pricing")
    assert code == 2, data
    assert (root / "laws" / "pricing.py").read_bytes() == law_before
    strat = (root / "laws" / "strategies.py").read_text(encoding="utf-8")
    assert "cups" in strat and "max_value=100" in strat  # addition in, change not applied
    assert not (root / "laws" / "drafts" / "strategies.py").exists()
    lines = "\n".join(data["lines"])
    assert "laws/pricing.py" in lines and "made the change a proposal" in lines
    assert "changes or removes existing generators: percents" in lines
    waiting = {w["file"]: w for w in data["data"]["waiting_changes"]}
    assert set(waiting) == {"laws/pricing.py", "laws/strategies.py"}
    assert waiting["laws/pricing.py"]["direction"] == "looser"
    assert waiting["laws/strategies.py"]["kind"] == "generators"
    assert clean(root)
    # the draft still runs, using the added generator
    assert data["data"]["drafts"][0]["probe"]["status"] == "holds"

    # --yes without ids approves the draft but never the drafting agent's edits
    code, data = lk(capsys, root, "approve", "--yes", "--by", "Ada")
    assert code == 2, data
    assert (root / "laws" / "pricing_total.py").is_file()
    assert {w["history_id"] for w in data["data"]["waiting"]} == {
        w["history_id"] for w in waiting.values()
    }
    assert (root / "laws" / "pricing.py").read_bytes() == law_before
    assert clean(root)


def test_draft_reject(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "failing")
    lk(capsys, root, "draft", "teashop.pricing")
    lock_before = (root / "laws.lock").read_text(encoding="utf-8")
    code, data = lk(capsys, root, "approve", "laws/drafts/pricing_one_cup.py", "--no", "--by", "Bo")
    assert code == 0, data
    assert not (root / "laws" / "drafts" / "pricing_one_cup.py").exists()
    e = last(root)
    assert (e.action, e.status, e.actor) == ("reject_draft", "rejected", "person:Bo")
    assert (root / "laws.lock").read_text(encoding="utf-8") == lock_before
    old = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = "200"
    try:
        text = lawchanges.render_since(root, None, integrity=[])
    finally:
        if old is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = old
    assert (
        "Rejected" in text and "“The total price is never more than the price of one cup”" in text
    )
    assert "rejected by Bo" in text


@pytest.mark.parametrize(
    "mode,message", [("nothing", "wrote no drafts"), ("crash", "stopped with an error")]
)
def test_draft_agent_wrote_nothing(tmp_path, capsys, monkeypatch, mode, message):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    code, data = lk(capsys, root, "draft", "teashop.pricing")
    assert code == 3 and message in data["summary"]


def test_draft_setup_problems_and_dry_run(tmp_path, capsys):
    root = make_project(tmp_path)
    (root / "lawkeep.toml").write_text("", encoding="utf-8")
    code, data = lk(capsys, root, "draft", "teashop.pricing")
    assert code == 3 and "No coding agent is set up" in data["summary"]
    code, data = lk(capsys, root, "draft", "teashop.nope")
    assert code == 3 and "Cannot find the module" in data["summary"]
    code, data = lk(capsys, root, "draft", "teashop.pricing", "--dry-run", "--request", "be nice")
    assert code == 0 and "be nice" in data["data"]["prompt"]


def test_draft_uses_quick_check_when_available(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    (root / "lawkeep.toml").write_text(
        (root / "lawkeep.toml").read_text().replace("quick_check = false", "quick_check = true")
    )
    import lawkeep.strength as strength

    def fake_quick(root_, cfg, *, law_ids=None, include_drafts=False):
        assert include_drafts is True
        return [
            StrengthResult(
                law_id=i,
                quick_check=[
                    QuickCheckOutcome("teashop.pricing:total", "always_zero", False),
                    QuickCheckOutcome("teashop.pricing:total", "raises", True),
                ],
                flags=["still passes when total() always returns 0"],
            )
            for i in law_ids or []
        ]

    monkeypatch.setattr(strength, "run_quick_check", fake_quick)
    monkeypatch.setenv("FAKE_AGENT_MODE", "good")
    code, data = lk(capsys, root, "draft", "teashop.pricing")
    assert code == 1  # flagged as weak: shown, the person decides
    assert data["data"]["drafts"][0]["quick_check_text"] == (
        "quick check: caught 1 of 2 dumb versions · still passes when total() always returns 0"
    )


def test_approve_interactive_and_no_terminal(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "good")
    lk(capsys, root, "draft", "teashop.pricing")
    # no terminal, no --yes: waiting
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: False)
    code, text = lk(capsys, root, "approve", as_json=False)
    assert code == 2 and "WAITING FOR YOUR DECISION" in text and "Follows" in text
    assert "Nothing was decided yet." in text and "Next step:" in text
    # a person in a terminal says yes
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: True)
    asked = []
    monkeypatch.setattr(approve_cmd, "_ask", lambda q: asked.append(q) or True)
    code, text = lk(capsys, root, "approve", "--by", "Ada", as_json=False)
    assert code == 0 and asked == ["Approve this law?"]
    assert "“The total price is never negative”" in text
    assert "law id pricing_total.total_never_negative" in text
    assert "Approved. “The total price is never negative” is now a law" in text
    assert "DONE" in text
    assert last(root).details["channel"] == "terminal"
    code, data = lk(capsys, root, "approve")
    assert code == 0 and "Nothing is waiting" in data["summary"]
    code, data = lk(capsys, root, "approve", "nope.law", "--yes")
    assert code == 3 and "Nothing waiting matches 'nope.law'" in data["summary"]


# ----------------------------------------------------------------------------------- propose


def propose(capsys, root, name, *extra):
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
        *extra,
    )


def test_propose_stricter_is_applied_automatically(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = propose(capsys, root, "stricter")
    assert code == 0, data
    assert data["decision"] == "applied" and data["direction"] == "stricter"
    assert data["permission_key"] == "add_law" and data["permission_value"] == "auto"
    assert (root / "laws" / "pricing.py").read_text() == (
        FIX / "proposals" / "stricter.py"
    ).read_text()
    e = last(root)
    assert (e.action, e.direction, e.permission_used, e.actor) == (
        "change_law",
        "stricter",
        "auto",
        "agent:codex",
    )
    assert e.files and "laws/pricing.py" in e.files
    assert clean(root)
    assert not (root / ".lawkeep" / "tmp").exists() or not any(
        (root / ".lawkeep" / "tmp").iterdir()
    )


def test_propose_same_meaning_is_applied_as_rename_target(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = propose(capsys, root, "renamed")
    assert code == 0, data
    assert data["direction"] == "same" and data["permission_key"] == "rename_target"
    assert last(root).action == "rename_target"


def test_propose_refactor_with_same_results_is_unclear_and_asks(tmp_path, capsys):
    """Same results on the cases tried, but more than a rename changed: a person decides."""
    root = make_project(tmp_path)
    code, data = propose(capsys, root, "same")
    assert code == 2, data
    assert data["direction"] == "unclear" and data["permission_key"] == "change_law"
    assert data["evidence"]["rename_only"] is False


def test_propose_small_bound_change_is_never_auto_applied(tmp_path, capsys):
    """`<= amount` -> `<= amount + 1` only matters on inputs today's code never produces, so
    the cases look the same; it must still wait for a person (found in the integration run)."""
    root = make_project(tmp_path)
    before = (root / "laws" / "pricing.py").read_text()
    code, data = propose(capsys, root, "looser")
    assert code == 2 and data["decision"] == "waiting", data
    assert data["direction"] in ("looser", "unclear")
    assert (root / "laws" / "pricing.py").read_text() == before


def test_propose_looser_waits_then_approve(tmp_path, capsys):
    root = make_project(tmp_path)
    before = (root / "laws" / "pricing.py").read_text()
    code, data = propose(capsys, root, "looser")
    assert code == 2, data
    assert data["decision"] == "waiting" and data["direction"] == "looser"
    assert data["permission_key"] == "weaken_law" and data["permission_value"] == "ask"
    assert any(r["old_holds"] is False and r["holds"] is True for r in data["examples"])
    assert (root / "laws" / "pricing.py").read_text() == before
    hid = data["history_id"]
    pdir = root / "laws" / "drafts" / "proposals" / hid
    assert (pdir / "proposed.py.txt").is_file() and (pdir / "meta.json").is_file()
    pending = history.pending(history.read(root))
    assert [p.id for p in pending] == [hid]
    assert pending[0].details["proposal_path"] == f"laws/drafts/proposals/{hid}/"
    assert clean(root)

    code, text = lk(capsys, root, "approve", as_json=False)
    assert "Proposed law change" in text
    assert "Looser — the law would allow more, so it protects less." in text
    assert "Why:" in text and "because looser" in text
    assert "Think twice: this makes the law protect less." in text
    assert "Old law" in text and "New law" in text

    code, data = lk(capsys, root, "approve", hid, "--yes", "--by", "Ada")
    assert code == 0, data
    assert (root / "laws" / "pricing.py").read_text() == (
        FIX / "proposals" / "looser.py"
    ).read_text()
    e = last(root)
    assert (e.refers_to, e.approved_by, e.status, e.direction) == (
        hid,
        "person:Ada",
        "applied",
        "looser",
    )
    assert e.actor == "agent:codex"
    assert history.pending(history.read(root)) == []
    assert not pdir.exists() and clean(root)


def test_propose_looser_rejected(tmp_path, capsys):
    root = make_project(tmp_path)
    before = (root / "laws" / "pricing.py").read_text()
    _, data = propose(capsys, root, "looser")
    code, _ = lk(capsys, root, "approve", data["history_id"], "--no", "--by", "Ada")
    assert code == 0
    assert (root / "laws" / "pricing.py").read_text() == before
    e = last(root)
    assert (e.status, e.refers_to, e.actor) == ("rejected", data["history_id"], "person:Ada")
    assert history.pending(history.read(root)) == []


def test_swapped_proposal_is_refused_on_approve(tmp_path, capsys):
    """A parked proposal changed on disk after it was recorded is not applied."""
    root = make_project(tmp_path)
    before = (root / "laws" / "pricing.py").read_text()
    _, data = propose(capsys, root, "looser")
    hid = data["history_id"]
    parked = root / "laws" / "drafts" / "proposals" / hid / "proposed.py.txt"
    parked.write_text(parked.read_text() + "\n# swapped after the person saw it\n")
    code, out = lk(capsys, root, "approve", hid, "--yes", "--by", "Ada")
    assert code != 0, out
    assert (root / "laws" / "pricing.py").read_text() == before
    assert [p.id for p in history.pending(history.read(root))] == [hid]


def test_propose_delete_always_asks(tmp_path, capsys):
    root = make_project(tmp_path, '\n[permissions]\nweaken_law = "auto"\nchange_law = "auto"\n')
    code, data = lk(capsys, root, "propose", "--delete", LAW_ID, "--reason", "obsolete")
    assert code == 2 and data["decision"] == "waiting"
    assert data["permission_key"] == "weaken_law" and data["direction"] == "looser"
    assert (root / "laws" / "pricing.py").is_file()
    code, _ = lk(capsys, root, "approve", data["history_id"], "--yes", "--by", "Ada")
    assert code == 0
    assert not (root / "laws" / "pricing.py").exists()
    e = last(root)
    assert e.action == "remove_law" and e.files == {"laws/pricing.py": None}
    assert clean(root)


def test_propose_skip_is_looser(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = propose(capsys, root, "skip")
    assert code == 2 and data["direction"] == "looser"
    assert last(root).action == "skip_law"
    assert "skip" in json.dumps(data["evidence"])


def test_propose_respects_ask_for_stricter(tmp_path, capsys):
    root = make_project(tmp_path, '\n[permissions]\nadd_law = "ask"\n')
    code, data = propose(capsys, root, "stricter")
    assert code == 2 and data["permission_value"] == "ask"


def test_propose_new_law_in_existing_file(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = lk(
        capsys,
        root,
        "propose",
        "pricing.zero_discount_is_identity",
        "--file",
        str(FIX / "proposals" / "added_law.py"),
        "--reason",
        "new rule",
    )
    assert code == 0, data
    assert data["direction"] == "stricter" and last(root).action == "add_law"
    locked = lock.read_lock(root)
    assert locked is not None and "pricing.zero_discount_is_identity" in locked.laws


def test_propose_broken_versions(tmp_path, capsys):
    root = make_project(tmp_path)
    code, data = propose(capsys, root, "import_error")
    assert code == 1 and data["decision"] == "rejected"
    assert last(root).status == "rejected"
    code, data = lk(
        capsys,
        root,
        "propose",
        LAW_ID,
        "--file",
        str(FIX / "proposals" / "broken_syntax.txt"),
        "--reason",
        "oops",
    )
    assert code == 3 and "syntax error" in data["summary"]


def test_propose_refuses_while_unrecorded_change(tmp_path, capsys):
    root = make_project(tmp_path)
    law = root / "laws" / "pricing.py"
    law.write_text(law.read_text() + "\n# edited by hand\n")
    n = len(history.read(root))
    code, data = propose(capsys, root, "stricter")
    assert code == 2 and "outside lawkeep" in data["summary"]
    assert len(history.read(root)) == n  # nothing recorded
    code, data = lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    assert code == 0, data
    assert last(root).action == "accept_unrecorded_change" and clean(root)


def test_proposed_draft_file_is_not_left_behind_as_a_new_draft(tmp_path, capsys, monkeypatch):
    """`propose --file laws/drafts/x.py` (relative to the root) parks the content and removes
    the draft file, so `approve` shows one proposal, not an extra draft with the same id."""
    root = make_project(tmp_path)
    draft = root / "laws" / "drafts" / "pricing_v2.py"
    draft.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / "proposals" / "looser.py", draft)
    monkeypatch.chdir(tmp_path)  # not the project root: the path is resolved against the root
    code, data = lk(
        capsys, root, "propose", LAW_ID, "--file", "laws/drafts/pricing_v2.py", "--reason", "x"
    )
    assert code == 2 and data["decision"] == "waiting", data
    assert not draft.exists()
    code, data = lk(capsys, root, "approve")
    assert code == 2
    waiting = json.dumps(data["data"]["waiting"]).lower().replace("drafts/proposals", "")
    assert "draft" not in waiting.replace('"draft_edit"', "")  # group names, not a new draft
    assert data["data"]["waiting_for_person"]["by_kind"]["draft"] == 0


def test_accept_a_hand_rewritten_lock(tmp_path, capsys):
    """Law edited AND laws.lock regenerated by hand: flagged, and a person can accept it."""
    from lawkeep import integrity

    root = make_project(tmp_path)
    law = root / "laws" / "pricing.py"
    law.write_text(law.read_text() + "\n# edited by hand\n")
    lock.write_lock(root, lock.build_lock(root))  # the cheat: refresh the lock by hand
    assert clean(root)
    kinds = {i.kind for i in integrity.check_integrity(root, config.load_config(root))}
    assert "lock_rewritten" in kinds
    code, data = lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    assert code == 0, data
    e = last(root)
    assert e.action == "accept_unrecorded_change" and "laws/pricing.py" in e.files
    kinds = {i.kind for i in integrity.check_integrity(root, config.load_config(root))}
    assert "lock_rewritten" not in kinds


def test_stale_proposal_is_not_applied(tmp_path, capsys):
    root = make_project(tmp_path)
    _, waiting = propose(capsys, root, "looser")
    code, _ = propose(capsys, root, "stricter")
    assert code == 0
    code, data = lk(capsys, root, "approve", waiting["history_id"], "--yes", "--by", "T")
    assert code == 3 and "changed after this proposal" in data["summary"]


# --------------------------------------------------------------------- direction and examples


def test_direction_vacuous_edit_is_looser(tmp_path):
    root = make_project(tmp_path)
    new = tmp_path / "vacuous.py"
    new.write_text(
        (FIX / "proposals" / "same.py")
        .read_text()
        .replace("assert result <= amount", "assert result == result")
    )
    res = direction.decide_direction(
        root, config.load_config(root), LAW_ID, root / "laws" / "pricing.py", new
    )
    assert res.direction == "looser", res.explanation
    assert res.evidence["broken_versions"] > 0


def test_direction_new_and_deleted_laws(tmp_path):
    root = make_project(tmp_path)
    cfg = config.load_config(root)
    assert (
        direction.decide_direction(root, cfg, "x.y", None, root / "laws" / "pricing.py").direction
        == "stricter"
    )
    assert (
        direction.decide_direction(root, cfg, LAW_ID, root / "laws" / "pricing.py", None).direction
        == "looser"
    )


def test_static_weakening_reasons():
    old = (
        "@law('x')\n@settings(max_examples=500)\n@given(a=st.integers())\n"
        "def f(a):\n    assert a == a\n    assert a + 0 == a\n"
    )
    new = (
        "@law('x')\n@settings(max_examples=5)\n@given(a=st.integers())\n"
        "def f(a):\n    assume(a > 0)\n    assert a == a\n"
    )
    reasons = direction.static_weakening(old, "f", new, "f", 200)
    assert any("fewer inputs (500 → 5 per run)" in r for r in reasons)
    assert any("removes checks (2 → 1 assert lines)" in r for r in reasons)
    assert any("assume()" in r for r in reasons)


def test_example_rows_for_an_approved_law(tmp_path):
    root = make_project(tmp_path)
    rows = examples_table.example_rows(
        root, config.load_config(root), root / "laws" / "pricing.py", LAW_ID, count=5
    )
    assert 1 <= len(rows) <= 5
    assert all(r.holds is True for r in rows)
    assert all(r.result and r.result.startswith("discount() → ") for r in rows)
    assert any(r.note and r.note.startswith("edge case") for r in rows)
    # no bytecode was written next to the laws
    assert not list((root / "laws").rglob("*.pyc"))


def test_probe_falls_back_for_fixture_laws(tmp_path):
    root = make_project(tmp_path)
    (root / "laws" / "fixture_law.py").write_text(
        "from lawkeep import law\n\n@law('uses a fixture')\ndef needs_db(db):\n    assert db\n"
    )
    p = examples_table.probe_law(
        root, config.load_config(root), root / "laws" / "fixture_law.py", "fixture_law.needs_db"
    )
    assert p.status == "not_run" and "pytest fixtures (db)" in (p.message or "")
