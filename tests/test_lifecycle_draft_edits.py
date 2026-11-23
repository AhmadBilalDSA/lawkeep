"""Edits the drafting agent makes to approved files become waiting proposals (draft_edits).

PLAN.md Decisions, 2026-11-22 "Drafting" (approved 2026-11-23 with a change): an edit the
agent started by `lawkeep draft` makes to an approved law, an existing generator or
lawkeep.toml is not undone and lost; the approved version goes back on disk and the edit
waits as a proposal. Approve applies it, reject drops it. Uses the fake agent and the tea
shop in tests/fixtures/lifecycle/.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from lawkeep import cli, direction, draft_edits, history, lawchanges, lock

FIX = Path(__file__).parent / "fixtures" / "lifecycle"


@pytest.fixture(autouse=True)
def _no_planted_bugs(monkeypatch):
    monkeypatch.setenv(direction.PLANTED_BUGS_ENV, "0")
    monkeypatch.setenv("COLUMNS", "200")  # human text unwrapped


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    agent = f"{sys.executable} {FIX / 'fake_agent.py'} {{prompt_file}}"
    (root / "lawkeep.toml").write_text(
        f'[agent]\nname = "other"\ncommand = "{agent}"\n\n[strength]\nquick_check = false\n',
        encoding="utf-8",
    )
    for name in ("history.jsonl", "failing_examples.jsonl"):
        (root / "laws" / name).write_text("", encoding="utf-8")
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def lk(capsys, root: Path, *args: str, as_json: bool = True):
    code = cli.main([*args, "--root", str(root)] + (["--json"] if as_json else []))
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


def clean(root: Path) -> bool:
    return lock.diff_against_lock(root).is_empty


STRICTER_MARK = "assert result >= 0"


def _draft(capsys, monkeypatch, root: Path, mode: str):
    monkeypatch.setenv("FAKE_AGENT_MODE", mode)
    return lk(capsys, root, "draft", "teashop.pricing")


def _gate_is_green(capsys, root: Path) -> dict:
    """check: every approved law holds and nothing changed outside lawkeep (the only thing
    waiting is the proposal, so the result is 2, never 1 or an unrecorded change)."""
    code, data = lk(capsys, root, "check")
    assert code == 2, data
    assert all(r["status"] == "holds" for r in data["laws"]) and data["laws"]
    errors = [i for i in data["integrity"] if i["severity"] == "error"]
    assert errors == [], errors
    assert data["waiting"]
    return data


@pytest.mark.parametrize(
    "mode,direction,needle",
    [("edit_looser", "looser", "<= amount + 5"), ("edit_stricter", "stricter", STRICTER_MARK)],
)
def test_law_edit_waits_and_approve_applies(tmp_path, capsys, monkeypatch, mode, direction, needle):
    root = make_project(tmp_path)
    law = root / "laws" / "pricing.py"
    before = law.read_bytes()
    code, data = _draft(capsys, monkeypatch, root, mode)
    assert code == 2, data

    # the approved version is back, nothing is unrecorded, the edit waits
    assert law.read_bytes() == before and clean(root)
    [w] = data["data"]["waiting_changes"]
    assert (w["file"], w["kind"], w["direction"]) == ("laws/pricing.py", "law", direction)
    assert w["law_id"] == "pricing.discount_never_raises_price"
    assert w["reason"].startswith(draft_edits.REASON)
    if mode == "edit_looser":
        assert "discounts may round up by a few cents" in w["reason"]  # the agent's own words
        assert w["examples"]  # inputs the new version would let through
    entry = next(e for e in history.pending(history.read(root)) if e.id == w["history_id"])
    assert entry.status == "waiting" and entry.permission_used == "ask"
    assert entry.actor == "agent:other" and entry.details["from_draft"] is True
    assert entry.details["whole_file"] is True and entry.details["proposal_sha256"]
    assert entry.details["base_sha256"] and entry.direction == direction
    proposal = root / entry.details["proposal_path"] / "proposed.py.txt"
    assert needle in proposal.read_text(encoding="utf-8")

    # check keeps testing the approved law; report lists the change as waiting
    gate = _gate_is_green(capsys, root)
    assert w["history_id"] in {x["history_id"] for x in gate["waiting"]}
    assert any(
        c.change == "waiting" and c.history_id == w["history_id"]
        for c in lawchanges.list_pending(root)
    )
    code, rep = lk(capsys, root, "report")
    assert any(
        c["change"] == "waiting" and c["history_id"] == w["history_id"] for c in rep["changes"]
    )

    # even a stricter edit is not decided by --yes without ids
    code, data = lk(capsys, root, "approve", "--yes", "--by", "Ada")
    assert code == 2 and law.read_bytes() == before

    code, data = lk(capsys, root, "approve", w["history_id"], "--yes", "--by", "Ada")
    assert code == 0, data
    assert needle in law.read_text(encoding="utf-8")
    applied = history.read(root)[-1]
    assert (applied.refers_to, applied.status, applied.approved_by) == (
        w["history_id"],
        "applied",
        "person:Ada",
    )
    assert applied.details.get("from_draft") is True
    assert clean(root)
    assert not (root / entry.details["proposal_path"]).exists()


def test_law_edit_reject_drops_it(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    law = root / "laws" / "pricing.py"
    before = law.read_bytes()
    code, data = _draft(capsys, monkeypatch, root, "edit_looser")
    [w] = data["data"]["waiting_changes"]
    code, data = lk(capsys, root, "approve", w["history_id"], "--no", "--by", "Bo")
    assert code == 0, data
    assert law.read_bytes() == before and clean(root)
    last = history.read(root)[-1]
    assert (last.refers_to, last.status) == (w["history_id"], "rejected")
    assert not history.pending(history.read(root))
    assert not (root / "laws" / "drafts" / "proposals" / w["history_id"]).exists()


def test_settings_edit_waits(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    toml = root / "lawkeep.toml"
    before = toml.read_bytes()
    code, data = _draft(capsys, monkeypatch, root, "edit_settings")
    assert code == 2, data
    assert toml.read_bytes() == before and clean(root)
    [w] = data["data"]["waiting_changes"]
    assert (w["file"], w["kind"], w["action"]) == ("lawkeep.toml", "settings", "change_settings")
    assert w["changes"] == ["permissions.add_law: added as 'ask'"]
    _gate_is_green(capsys, root)

    # the human screen names it plainly
    code, text = lk(capsys, root, "approve", w["history_id"], as_json=False)
    assert "Settings (lawkeep.toml)" in text and "permissions.add_law" in text
    assert "drafting AI" in text

    code, data = lk(capsys, root, "approve", w["history_id"], "--yes", "--by", "Ada")
    assert code == 0, data
    assert 'add_law = "ask"' in toml.read_text(encoding="utf-8")
    assert clean(root)


def test_generator_edit_waits_and_additions_still_go_in(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    strat = root / "laws" / "strategies.py"
    code, data = _draft(capsys, monkeypatch, root, "edit_generator")
    assert code == 2, data
    text = strat.read_text(encoding="utf-8")
    assert "def cups" in text and "max_value=100" in text and clean(root)
    assert [e.action for e in history.read(root)].count("add_strategy") == 1
    [w] = data["data"]["waiting_changes"]
    assert (w["file"], w["kind"], w["action"]) == (
        "laws/strategies.py",
        "generators",
        "change_strategy",
    )
    assert "changes or removes existing generators: percents" in w["changes"]
    assert data["data"]["drafts"][0]["probe"]["status"] == "holds"  # uses the added cups()
    _gate_is_green(capsys, root)

    code, data = lk(capsys, root, "approve", w["history_id"], "--yes", "--by", "Ada")
    assert code == 0, data
    text = strat.read_text(encoding="utf-8")
    assert "max_value=1)" in text and "def cups" in text and clean(root)


def test_target_changed_meanwhile_is_refused(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    code, data = _draft(capsys, monkeypatch, root, "edit_stricter")
    [w] = data["data"]["waiting_changes"]
    law = root / "laws" / "pricing.py"
    law.write_text(law.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    code, data = lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    assert code == 0 and clean(root), data
    code, data = lk(capsys, root, "approve", w["history_id"], "--yes", "--by", "Ada")
    assert code == 3, data
    assert "changed after this proposal was made" in " ".join(data["data"]["problems"])


def test_agent_only_edited_an_approved_law(tmp_path, capsys, monkeypatch):
    """No drafts but an edit: not "wrote nothing" (3); the edit waits (2)."""
    root = make_project(tmp_path)
    code, data = _draft(capsys, monkeypatch, root, "edit_only")
    assert code == 2, data
    assert data["data"]["drafts"] == [] and len(data["data"]["waiting_changes"]) == 1
    assert "wrote no new drafts" in data["summary"]
    _gate_is_green(capsys, root)


def test_deleted_law_file_waits_and_approve_deletes(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    law = root / "laws" / "pricing.py"
    code, data = _draft(capsys, monkeypatch, root, "delete_law")
    assert code == 2, data
    assert law.is_file() and clean(root)
    [w] = data["data"]["waiting_changes"]
    assert (w["action"], w["direction"], w["deleted"]) == ("remove_law", "looser", True)
    code, data = lk(capsys, root, "approve", w["history_id"], "--yes", "--by", "Ada")
    assert code == 0, data
    assert not law.exists() and clean(root)


def test_draft_screen_lists_the_waiting_changes(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("FAKE_AGENT_MODE", "edit_looser")
    code, text = lk(capsys, root, "draft", "teashop.pricing", as_json=False)
    assert code == 2
    assert (
        "The drafting AI also changed approved laws or settings — waiting for your review" in text
    )
    assert "Looser" in text and "“A discount never makes the price larger”" in text
    assert "not even a stricter change" in " ".join(text.split())


def test_agent_reasons_and_additions_only():
    out = draft_edits.agent_reasons(
        "Done.\n- Changed `laws/pricing.py`: rounding\nChanged lawkeep.toml: speed\n"
    )
    assert out == {"laws/pricing.py": "rounding", "lawkeep.toml": "speed"}
    old = "from hypothesis import strategies as st\n\n\ndef a():\n    return st.just(1)\n"
    new = (
        "import math\nfrom hypothesis import strategies as st\n\n\ndef a():\n"
        "    return st.just(2)\n\n\ndef b():\n    return st.just(math.pi)\n"
    )
    text = draft_edits.additions_only(old, new, ["b"])
    assert text is not None and "st.just(1)" in text and "def b" in text
    assert "import math" in text and "st.just(2)" not in text
    assert draft_edits.settings_changes(
        "[check]\nexamples = 200\n", "[check]\nexamples = 20\n"
    ) == ["check.examples: 200 → 20"]
