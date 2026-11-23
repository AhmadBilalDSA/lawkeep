"""Tests for the fixes from the v0.1 review (law lifecycle: direction, approve, propose).

Each test would have caught one verified finding. Made-up tea shop project only; no real
coding agent, no network.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lawkeep import cli, direction, history, ledger, lock
from lawkeep.commands import approve as approve_cmd
from lawkeep.errors import HistoryError

FIX = Path(__file__).parent / "fixtures" / "lifecycle"
LAW_ID = "pricing.discount_never_raises_price"

BASE_LAW = """from teashop.pricing import discount

from lawkeep import given, law, st


@law("A discount never makes the price larger")
@given(amount=st.money(max_value=1000), percent=st.integers(min_value=0, max_value=100))
def discount_never_raises_price(amount, percent):
    assert discount(amount, percent) <= amount
"""


@pytest.fixture(autouse=True)
def _no_planted_bugs(monkeypatch):
    monkeypatch.setenv(direction.PLANTED_BUGS_ENV, "0")


def make_project(tmp_path: Path, *, quick_check: bool = False, git: bool = False) -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    (root / "lawkeep.toml").write_text(
        f'[agent]\nname = "other"\n\n[strength]\nquick_check = {str(quick_check).lower()}\n',
        encoding="utf-8",
    )
    for name in ("history.jsonl", "failing_examples.jsonl"):
        (root / "laws" / name).write_text("", encoding="utf-8")
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    if git:
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.invalid"],
            ["config", "user.name", "Tester"],
            ["add", "-A"],
            ["commit", "-q", "-m", "start"],
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def lk(capsys, root: Path, *args: str):
    code = cli.main([*args, "--root", str(root), "--json"])
    return code, json.loads(capsys.readouterr().out)


def write_draft(root: Path, name: str, text: str) -> Path:
    p = root / "laws" / "drafts" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def propose_text(capsys, root: Path, text: str, law_id: str = LAW_ID, name: str = "v2.py"):
    p = write_draft(root, name, text)
    return lk(capsys, root, "propose", law_id, "--file", str(p), "--reason", "test")


# ----------------------------------------------------------------- rename_only (alpha-equiv.)

OLD_REFUND = """
@law("A refund never exceeds the charge")
@given(charge=st.charges(), amount=st.money())
def capped(charge, amount):
    result = refund(charge, amount)
    assert result.total <= charge.total
"""


def test_rename_only_rejects_two_names_merged_into_one():
    """'result.total <= charge.total' -> 'result.total <= result.total' is a tautology."""
    new = OLD_REFUND.replace("<= charge.total", "<= result.total")
    assert not direction.rename_only(OLD_REFUND, "capped", new, "capped")


def test_rename_only_accepts_a_consistent_local_rename():
    new = OLD_REFUND.replace("result", "outcome")
    assert direction.rename_only(OLD_REFUND, "capped", new, "capped")


def test_rename_only_rejects_changed_generators_and_keywords():
    gen = BASE_LAW.replace("percent=st.integers(", "percent=st.booleans(", 1)
    assert not direction.rename_only(
        BASE_LAW, "discount_never_raises_price", gen, "discount_never_raises_price"
    )
    kw = BASE_LAW.replace("max_value=1000", "min_value=1000", 1)
    assert not direction.rename_only(
        BASE_LAW, "discount_never_raises_price", kw, "discount_never_raises_price"
    )


def test_rename_only_rejects_attribute_and_builtin_renames():
    attr = OLD_REFUND.replace("charge.total", "charge.fee")
    assert not direction.rename_only(OLD_REFUND, "capped", attr, "capped")
    old = "def f(x):\n    assert abs(x) >= 0\n"
    new = "def f(x):\n    assert max(x) >= 0\n"
    assert not direction.rename_only(old, "f", new, "f")


def test_rename_only_outside_name_must_be_gone_unless_it_is_an_alias():
    old = "from shop import refund\n" + OLD_REFUND
    new = "from shop import refund_v2\n" + OLD_REFUND.replace("refund(", "refund_v2(")
    assert direction.rename_only(old, "capped", new, "capped", still_defined=lambda n: False)
    # the old function still exists: pointing the law at another function is not a rename
    assert not direction.rename_only(old, "capped", new, "capped", still_defined=lambda n: True)
    alias = "from shop import refund as give_back\n" + OLD_REFUND.replace("refund(", "give_back(")
    assert direction.rename_only(old, "capped", alias, "capped", still_defined=lambda n: True)


def test_generators_changed_and_examples():
    narrow = BASE_LAW.replace(
        "percent=st.integers(min_value=0, max_value=100)", "percent=st.just(0)"
    )
    assert direction.generators_changed(
        BASE_LAW, "discount_never_raises_price", narrow, "discount_never_raises_price"
    )
    with_example = BASE_LAW.replace("@given(", "@example(amount=1, percent=0)\n@given(", 1)
    assert not direction.generators_changed(
        BASE_LAW, "discount_never_raises_price", with_example, "discount_never_raises_price"
    )


def _bound(old: str, new: str) -> str:
    found = direction.bound_direction(old, "f", new, "f")
    assert found is not None
    return found[0]


def test_bound_direction_reads_a_changed_limit():
    old = 'def f(x):\n    assert abs(x) <= Decimal("0.005")\n'
    looser = old.replace("0.005", "0.05")
    assert _bound(old, looser) == "looser"
    assert _bound(looser, old) == "stricter"
    flipped = "def f(x):\n    assert x >= 10\n"
    assert _bound(flipped, flipped.replace("10", "5")) == "looser"
    assert direction.bound_direction(old, "f", old.replace("abs", "int"), "f") is None


# ------------------------------------------------------------------------- propose (flows)


def test_narrowed_generator_plus_extra_assert_is_not_auto_applied(tmp_path, capsys):
    root = make_project(tmp_path)
    narrowed = (
        BASE_LAW.replace("percent=st.integers(min_value=0, max_value=100)", "percent=st.just(0)")
        + "    assert discount(amount, percent) >= 0\n"
    )
    code, data = propose_text(capsys, root, narrowed)
    assert code == 2, data
    assert data["decision"] == "waiting" and data["direction"] == "unclear"
    assert "st.just(0)" not in (root / "laws" / "pricing.py").read_text()


def test_generator_swap_is_not_applied_as_same_meaning(tmp_path, capsys):
    root = make_project(tmp_path)
    swapped = BASE_LAW.replace(
        "percent=st.integers(min_value=0, max_value=100)", "percent=st.booleans()"
    )
    code, data = propose_text(capsys, root, swapped)
    assert code == 2, data
    assert data["direction"] != "same" and data["decision"] == "waiting"
    assert "booleans" not in (root / "laws" / "pricing.py").read_text()


def test_bigger_limit_is_labelled_looser(tmp_path, capsys):
    root = make_project(tmp_path)
    looser = BASE_LAW.replace("<= amount", '<= amount + Decimal("0.05")').replace(
        "from teashop", "from decimal import Decimal\n\nfrom teashop"
    )
    base = BASE_LAW.replace("<= amount", '<= amount + Decimal("0.005")').replace(
        "from teashop", "from decimal import Decimal\n\nfrom teashop"
    )
    (root / "laws" / "pricing.py").write_text(base, encoding="utf-8")
    history.record_change(root, action="relock", actor="person:tester")
    code, data = propose_text(capsys, root, looser)
    assert code == 2 and data["direction"] == "looser", data


def test_proposing_the_same_file_again_records_nothing(tmp_path, capsys):
    root = make_project(tmp_path)
    before = len(history.read(root))
    code, data = propose_text(capsys, root, BASE_LAW)
    assert code == 0 and data["decision"] == "unchanged", data
    assert len(history.read(root)) == before


def test_refused_proposal_keeps_the_proposed_file(tmp_path, capsys):
    root = make_project(tmp_path)
    with (root / "laws" / "strategies.py").open("a", encoding="utf-8") as f:
        f.write("\n# hand edit\n")
    stricter = BASE_LAW + "    assert discount(amount, percent) >= 0\n"
    code, data = propose_text(capsys, root, stricter)
    assert code == 2 and data["decision"] == "refused", data
    assert (root / "laws" / "drafts" / "v2.py").is_file()
    assert data["history_id"] is None


VACUOUS = """from teashop.pricing import total

from lawkeep import given, law, st


@law("A total is always fine", id="pricing_vac.total_always_fine")
@given(price=st.money(max_value=100), quantity=st.integers(min_value=0, max_value=10))
def total_always_fine(price, quantity):
    total(price, quantity)
    assert True
"""


def test_new_vacuous_law_is_not_auto_applied(tmp_path, capsys):
    root = make_project(tmp_path, quick_check=True)
    code, data = propose_text(
        capsys, root, VACUOUS, law_id="pricing_vac.total_always_fine", name="pricing_vac.py"
    )
    assert code == 2, data
    assert data["decision"] == "waiting"
    assert "quick strength check" in data["summary"]
    assert not (root / "laws" / "pricing_vac.py").exists()
    entry = history.read(root)[-1]
    assert entry.status == "waiting" and entry.details["evidence"].startswith("Weak law")


STRONG_NEW = """from teashop.pricing import total

from lawkeep import given, law, st


@law("Ten cups cost ten times one cup", id="pricing_ten.ten_cups")
@given(price=st.money(max_value=100))
def ten_cups(price):
    assert total(price, 10) == price * 10
    assert total(price, 0) == 0
"""


def test_new_strong_law_is_applied_with_evidence(tmp_path, capsys):
    root = make_project(tmp_path, quick_check=True)
    code, data = propose_text(
        capsys, root, STRONG_NEW, law_id="pricing_ten.ten_cups", name="pricing_ten.py"
    )
    assert code == 0, data
    entry = history.read(root)[-1]
    assert entry.action == "add_law" and entry.details["evidence"].startswith("Strong law")


# ------------------------------------------------------------------------------------ approve


def _park_looser(capsys, root):
    looser = (FIX / "proposals" / "looser.py").read_text(encoding="utf-8")
    code, data = propose_text(capsys, root, looser, name="looser.py")
    assert code == 2
    return data["history_id"]


def test_accept_unrecorded_yes_decides_only_the_hand_edit(tmp_path, capsys):
    root = make_project(tmp_path)
    hid = _park_looser(capsys, root)
    write_draft(
        root, "pricing_total.py", STRONG_NEW.replace("pricing_ten.ten_cups", "pricing_total.x")
    )
    with (root / "laws" / "strategies.py").open("a", encoding="utf-8") as f:
        f.write("\n# hand edit\n")
    code, data = lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    assert code == 0, data
    assert [d["kind"] for d in data["data"]["decided"]] == ["unrecorded"]
    assert any(e.id == hid for e in history.pending(history.read(root)))
    assert (root / "laws" / "drafts" / "pricing_total.py").is_file()


def test_yes_without_ids_holds_back_weakening_proposals(tmp_path, capsys):
    root = make_project(tmp_path)
    hid = _park_looser(capsys, root)
    code, data = lk(capsys, root, "approve", "--yes", "--by", "Ada")
    assert code == 2, data
    assert any(e.id == hid for e in history.pending(history.read(root)))
    code, data = lk(capsys, root, "approve", hid, "--yes", "--by", "Ada")
    assert code == 0, data


def test_yes_without_terminal_needs_channel_or_name(tmp_path, capsys, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setattr(approve_cmd, "_interactive", lambda: False)
    hid = _park_looser(capsys, root)
    code, data = lk(capsys, root, "approve", hid, "--yes")
    assert code == 2 and not data["data"]["decided"], data
    assert any(e.id == hid for e in history.pending(history.read(root)))
    code, data = lk(capsys, root, "approve", hid, "--yes", "--channel", "agent permission prompt")
    assert code == 0, data
    e = history.read(root)[-1]
    assert e.approved_by == "person:unnamed"  # never the git user name
    assert e.details["terminal"] is False and e.details["channel"] == "agent permission prompt"


def test_unrecorded_review_labels_a_loosening_hand_edit(tmp_path, capsys):
    root = make_project(tmp_path, git=True)
    law = root / "laws" / "pricing.py"
    law.write_text(
        law.read_text().replace("    assert discount(amount, percent) <= amount\n", "    pass\n"),
        encoding="utf-8",
    )
    code, data = lk(capsys, root, "approve", "--accept-unrecorded")
    item = data["data"]["waiting"][0]
    assert item["kind"] == "unrecorded" and item["review"]["weakening"], item
    code, data = lk(capsys, root, "approve", "--accept-unrecorded", "--yes", "--by", "Ada")
    assert code == 0
    e = history.read(root)[-1]
    assert e.direction == "looser" and LAW_ID in (e.law_id or "")
    assert "looks looser" in e.details["evidence"]
    assert e.details["terminal"] is False


def test_non_utf8_draft_gets_a_clear_message(tmp_path, capsys):
    root = make_project(tmp_path)
    p = root / "laws" / "drafts" / "latin.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"# caf\xe9\n" + STRONG_NEW.encode())
    code, data = lk(capsys, root, "approve", "laws/drafts/latin.py", "--yes", "--by", "T")
    assert code == 3
    assert "UTF-8" in data["summary"] and ".." not in data["summary"]


# ------------------------------------------------------------------------------------ history


def _locked(root: Path) -> lock.LockFile:
    found = lock.read_lock(root)
    assert found is not None
    return found


def test_record_change_refuses_after_saved_examples_were_removed(tmp_path):
    root = make_project(tmp_path)
    ledger.add(root, LAW_ID, {"amount": "1", "percent": "0"}, found_by="lawkeep")
    history.record_change(root, action="add_example", actor="lawkeep", law_id=LAW_ID)
    assert _locked(root).append_only["laws/failing_examples.jsonl"].lines == 1
    (root / "laws" / "failing_examples.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(HistoryError, match="lost lines"):
        history.record_change(root, action="add_example", actor="agent:other", law_id=LAW_ID)
    assert _locked(root).append_only["laws/failing_examples.jsonl"].lines == 1
