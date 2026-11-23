"""Law-change direction: the static "limit moved" rule and deterministic evidence.

Fix for the flaky `<= amount + Decimal("0.005")` -> `<= amount + Decimal("0.05")` label
(sometimes LOOSER, sometimes UNCLEAR). Made-up tea shop project only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lawkeep import cli, config, direction, examples_table, history

FIX = Path(__file__).parent / "fixtures" / "lifecycle"
LAW_ID = "pricing.discount_never_raises_price"

LAW = """from decimal import Decimal

from teashop.pricing import discount

from lawkeep import given, law, st


@law("A discount never makes the price larger")
@given(amount=st.money(max_value=1000), percent=st.integers(min_value=0, max_value=100))
def discount_never_raises_price(amount, percent):
    assert discount(amount, percent) <= amount + Decimal("0.005")
"""


@pytest.fixture(autouse=True)
def _no_planted_bugs(monkeypatch):
    monkeypatch.setenv(direction.PLANTED_BUGS_ENV, "0")


def make_project(tmp_path: Path, law: str = LAW) -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    (root / "lawkeep.toml").write_text(
        '[agent]\nname = "other"\n\n[strength]\nquick_check = false\n', encoding="utf-8"
    )
    for name in ("history.jsonl", "failing_examples.jsonl"):
        (root / "laws" / name).write_text("", encoding="utf-8")
    (root / "laws" / "pricing.py").write_text(law, encoding="utf-8")
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def propose_text(capsys, root: Path, text: str):
    draft = root / "laws" / "drafts" / "v2.py"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(text, encoding="utf-8")
    code = cli.main(
        ["propose", LAW_ID, "--file", str(draft), "--reason", "test", "--root", str(root), "--json"]
    )
    return code, json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------ the formerly flaky change


@pytest.mark.parametrize("run", range(30))
def test_widened_limit_is_looser_every_run(tmp_path, capsys, run):
    """Was LOOSER or UNCLEAR at random (about half the runs); 30 runs must all agree."""
    root = make_project(tmp_path)
    code, data = propose_text(capsys, root, LAW.replace('"0.005"', '"0.05"'))
    assert code == 2 and data["direction"] == "looser", data
    assert data["decision"] == "waiting"


def test_narrowed_limit_is_stricter_and_applied(tmp_path, capsys):
    root = make_project(tmp_path, LAW.replace('"0.005"', '"0.05"'))
    code, data = propose_text(capsys, root, LAW)
    assert code == 0 and data["direction"] == "stricter", data
    assert data["decision"] == "applied"


def test_same_change_gets_the_same_label_and_evidence(tmp_path):
    """A change only the evidence can judge (a different check): identical results twice."""
    root = make_project(tmp_path)
    new = tmp_path / "v2.py"
    new.write_text(
        LAW.replace(
            'assert discount(amount, percent) <= amount + Decimal("0.005")',
            "assert discount(amount, percent) <= amount or percent > 90",
        ),
        encoding="utf-8",
    )
    cfg = config.load_config(root)
    old = root / "laws" / "pricing.py"
    first = direction.decide_direction(root, cfg, LAW_ID, old, new).to_dict()
    second = direction.decide_direction(root, cfg, LAW_ID, old, new).to_dict()
    assert first == second
    assert first["evidence"]["cases"] == second["evidence"]["cases"] > 0


def test_direction_seed_depends_on_law_and_both_versions():
    s = direction.direction_seed("a.b", "old", "new")
    assert s == direction.direction_seed("a.b", "old", "new")
    assert s != direction.direction_seed("a.c", "old", "new")
    assert s != direction.direction_seed("a.b", "old", "new2")
    assert s != direction.direction_seed("a.b", "new", "old")


def test_probe_seed_follows_file_contents(tmp_path):
    f = tmp_path / "law.py"
    f.write_text("x = 1\n", encoding="utf-8")
    versions = [{"name": "law", "file": str(f), "qualname": "x"}]
    s = examples_table.probe_seed(versions)
    assert s == examples_table.probe_seed(versions)
    f.write_text("x = 2\n", encoding="utf-8")
    assert s != examples_table.probe_seed(versions)


def test_probe_draws_the_same_inputs_every_run(tmp_path):
    root = make_project(tmp_path)
    cfg = config.load_config(root)
    versions = [
        {
            "name": "law",
            "file": str(root / "laws" / "pricing.py"),
            "qualname": "discount_never_raises_price",
        }
    ]
    runs = [examples_table.run_probe(root, cfg, versions, examples=40) for _ in range(2)]
    assert [c["inputs"] for c in runs[0]["cases"]] == [c["inputs"] for c in runs[1]["cases"]]
    assert not (root / ".hypothesis").exists()  # Hypothesis' cache stays out of the project


# ------------------------------------------------------------------ the static rule, unit level


def _src(line: str) -> str:
    return f"def f(x, y, b):\n    {line}\n"


def bound(old: str, new: str) -> str | None:
    found = direction.bound_direction(_src(old), "f", _src(new), "f")
    return found[0] if found else None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("assert x <= b", "assert x <= b + 5"),
        ("assert x <= b", 'assert x <= b + Decimal("0.05")'),
        ("assert x <= b", "assert x <= b + 0.5"),
        ("assert x <= b", "assert x <= 2 + b"),
        ("assert x <= b", "assert x <= b - (-5)"),
        ("assert x <= b", "assert x <= b - -5"),
        ('assert x <= b + Decimal("0.005")', 'assert x <= b + Decimal("0.05")'),
        ("assert x <= b - 2", "assert x <= b"),
        ("assert x >= b", "assert x >= b - 1"),
        ("assert x >= b", 'assert x >= b - float("1e-3")'),
        ("assert x < b", "assert x <= b"),
        ("assert x > b", "assert x >= b"),
        ("assert x == b", "assert x <= b"),
        ("assert x == b", "assert x >= b"),
        ("assert x == b", "assert x <= b + 1"),
        ("assert b >= x", "assert b + 1 >= x"),  # the limit on the left
        ("assert x <= 5", "assert x <= 10"),
        ("assert x >= 10", "assert x >= 5"),
        ('assert abs(x) <= Decimal("0.005")', 'assert abs(x) <= Decimal("0.05")'),
        ("assert x <= b and y > 0", "assert x <= b + 1 and y > 0"),
        ("assert x <= b or y > 0", "assert x < b + 1 or y > 0"),
        ("assert x <= b, 'too big'", "assert x <= b + 1, 'too big'"),
    ],
)
def test_widened_limits_are_looser_and_the_reverse_is_stricter(old, new):
    assert bound(old, new) == "looser"
    assert bound(new, old) == "stricter"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("assert x <= b", "assert x < b"),
        ("assert x <= b", "assert x == b"),
        ("assert x <= b", "assert x <= b - 1"),
        ("assert x >= b", "assert x >= b + 0.01"),
        ("assert x < b + 1", "assert x <= b"),
    ],
)
def test_narrowed_limits_are_stricter(old, new):
    assert bound(old, new) == "stricter"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("assert x <= b", "assert x >= b"),  # flipped: neither contains the other
        ("assert x == b", "assert x <= b - 1"),  # {b} vs (-inf, b-1]: disjoint
        ("assert x <= b", "assert x <= b * 2"),  # multiplication: smaller when b < 0
        ("assert x <= b", "assert x <= b + y"),  # not a constant
        ("assert x <= b", "assert y <= b + 1"),  # both sides changed
        ("assert not x <= b", "assert not x <= b + 1"),  # under `not`: meaning inverted
        ("assert x <= b, 'a'", "assert x <= b, 'b'"),  # only the message changed
        ("assert x != b", "assert x != b + 1"),  # != is not an interval
        ("assert 0 <= x <= b", "assert 0 <= x <= b + 1"),  # chained comparison
        ("assert x <= b", "assert x <= b + 0"),  # same limit written differently
        ("assert x <= b", 'assert x <= b + Decimal("NaN")'),  # not a finite number
        ("assert x <= b and y <= 1", "assert x <= b + 1 and y <= 2"),  # two comparisons
        ("assert (x if x <= 5 else 0) == 0", "assert (x if x <= 6 else 0) == 0"),
    ],
)
def test_undecidable_changes_are_left_to_the_evidence(old, new):
    assert bound(old, new) is None
    assert bound(new, old) is None


def test_comparison_outside_an_assert_or_inverted_is_not_judged():
    in_if = "def f(x):\n    if x <= 5:\n        assert x > 0\n"
    assert direction.bound_direction(in_if, "f", in_if.replace("5", "10"), "f") is None
    in_try = (
        "def f(x):\n    try:\n        assert x <= 5\n    except AssertionError:\n        pass\n"
    )
    assert direction.bound_direction(in_try, "f", in_try.replace("5", "10"), "f") is None
    in_raises = "def f(x):\n    with raises(AssertionError):\n        assert x <= 5\n"
    assert direction.bound_direction(in_raises, "f", in_raises.replace("5", "10"), "f") is None


def test_reason_names_both_checks():
    found = direction.bound_direction(_src("assert x <= b"), "f", _src("assert x <= b + 1"), "f")
    assert found is not None
    assert "`x <= b`" in found[1] and "`x <= b + 1`" in found[1]
    assert "allows more" in found[1]
