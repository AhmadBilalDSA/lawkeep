"""The strength check end to end on made-up projects (each run starts an isolated pytest
process; no coding agent, no network)."""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from lawkeep import config
from lawkeep.strength import catch_matrix, core, run_quick_check, run_strength

FIXTURE = Path(__file__).parent / "fixtures" / "strength" / "shop"


def make_project(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    for rel, text in (extra or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def by_id(results):
    return {r.law_id: r for r in results}


CART = """
    class Cart:
        def __init__(self, items):
            self.items = list(items)

        def total(self) -> int:
            return sum(self.items)

        @staticmethod
        def tax(amount: int) -> int:
            return amount // 5
"""


def test_one_sided_law_is_flagged_and_strong_law_is_not(tmp_path):
    root = make_project(tmp_path)
    before = snapshot(root)
    report = run_strength(root, config.load_config(root))
    laws = by_id(report.laws)

    capped = laws["refunds.refund_is_capped"]
    assert capped.functions_touched == ["shop.refunds:refund"]
    zero = next(q for q in capped.quick_check if q.variant == "always_zero")
    assert zero.caught is False  # "refund always 0" still passes the law
    assert any(f.startswith("One-sided") and "refund() always returns 0" in f for f in capped.flags)
    assert any(q.variant == "raises" and q.caught for q in capped.quick_check)
    assert not capped.vacuous

    fee = laws["fees.fee_is_two_percent"]  # calls refunds.apply_fee through the module
    assert fee.functions_touched == ["shop.refunds:apply_fee"]
    assert all(q.caught for q in fee.quick_check)
    assert not any(f.startswith("One-sided") for f in fee.flags)
    assert fee.planted_bugs_total and fee.planted_bugs_caught

    assert report.uncovered_functions == ["shop.refunds:loyalty_points"]
    assert report.exit_code == 1
    assert report.summary.startswith("SOME LAWS ARE WEAK")
    # nothing in the project was written (no .hypothesis, no __pycache__, no edits)
    assert snapshot(root) == before


def test_vacuous_failing_and_weak_data_laws(tmp_path):
    root = make_project(
        tmp_path,
        {
            "laws/odd.py": """
                from lawkeep import assume, given, law, st
                from shop.refunds import loyalty_points


                @law("Comparing a number with itself is always true")
                @given(n=st.integers())
                def checks_nothing(n):
                    assert n == n


                @law("Points never exceed the total")
                @given(n=st.integers(min_value=0, max_value=10_000))
                def mostly_thrown_away(n):
                    assume(n % 10 == 3)
                    assert loyalty_points(n) == n // 10


                @law("Points are always zero (wrong on purpose)")
                @given(n=st.integers(min_value=100, max_value=200))
                def wrong_law(n):
                    assert loyalty_points(n) == 0
            """,
        },
    )
    report = run_strength(root, config.load_config(root), law_ids=None)
    laws = by_id(report.laws)
    nothing = laws["odd.checks_nothing"]
    assert nothing.vacuous and nothing.functions_touched == []
    assert any(f.startswith("Vacuous") for f in nothing.flags)

    thrown = laws["odd.mostly_thrown_away"]
    assert any(f.startswith("Weak test data") for f in thrown.flags), thrown.flags

    wrong = laws["odd.wrong_law"]
    assert any("does not hold on today's code" in f for f in wrong.flags)
    assert wrong.quick_check == [] and wrong.planted_bugs_total is None
    assert "shop.refunds:loyalty_points" not in report.uncovered_functions


def test_methods_staticmethods_and_state_machines(tmp_path):
    root = make_project(
        tmp_path,
        {
            "src/shop/cart.py": CART,
            "laws/cart.py": """
                from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

                from lawkeep import given, law, st
                from shop.cart import Cart


                @law("Tax is never negative")
                @given(amount=st.integers(min_value=0, max_value=1000))
                def tax_not_negative(amount):
                    assert Cart.tax(amount) >= 0


                @law("A cart's total is the sum of what was added")
                class CartMachine(RuleBasedStateMachine):
                    def __init__(self):
                        super().__init__()
                        self.items = []

                    @rule(x=st.integers(min_value=1, max_value=100))
                    def add(self, x):
                        self.items.append(x)

                    @invariant()
                    def total_matches(self):
                        assert Cart(self.items).total() == sum(self.items)
            """,
        },
    )
    laws = by_id(
        run_quick_check(
            root, config.load_config(root), law_ids=["cart.tax_not_negative", "cart.CartMachine"]
        )
    )
    tax = laws["cart.tax_not_negative"]
    assert tax.functions_touched == ["shop.cart:Cart.tax"]
    assert any(f.startswith("One-sided") for f in tax.flags)  # "tax always 0" still holds

    machine = laws["cart.CartMachine"]
    assert "shop.cart:Cart.total" in machine.functions_touched
    total_checks = [q for q in machine.quick_check if q.function == "shop.cart:Cart.total"]
    assert total_checks and all(q.caught for q in total_checks if q.variant != "always_empty")
    assert not any(f.startswith("One-sided") for f in machine.flags)


def test_drafts_and_duplicate_ids(tmp_path):
    root = make_project(
        tmp_path,
        {
            "laws/drafts/refunds.py": """
                from lawkeep import given, law, st
                from shop.refunds import refund


                @law("A refund gives back exactly the amount when it fits in the charge")
                @given(charge=st.money(), amount=st.money())
                def refund_is_capped(charge, amount):
                    expected = amount if amount <= charge else charge
                    assert refund(charge, amount) == expected
            """,
        },
    )
    cfg = config.load_config(root)
    without = by_id(run_quick_check(root, cfg))
    assert "refunds.refund_is_capped" in without

    results = by_id(run_quick_check(root, cfg, include_drafts=True))
    draft = results["refunds.refund_is_capped@laws/drafts/refunds.py"]
    approved = results["refunds.refund_is_capped@laws/refunds.py"]
    assert not any(f.startswith("One-sided") for f in draft.flags)
    assert any(f.startswith("One-sided") for f in approved.flags)


def test_catch_matrix_and_endless_loop_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "RUN_TIMEOUT_FLOOR", 1.0)
    root = make_project(
        tmp_path,
        {
            "src/shop/loop.py": """
                def countdown(n: int) -> int:
                    steps = 0
                    while n > 0:
                        n -= 1
                        steps += 1
                    return steps
            """,
            "laws/loop.py": """
                from lawkeep import given, law, st
                from shop.loop import countdown


                @law("Counting down from n takes n steps")
                @given(n=st.integers(min_value=0, max_value=30))
                def countdown_steps(n):
                    assert countdown(n) == n
            """,
        },
    )
    cfg = config.load_config(root)
    m = catch_matrix(
        root, cfg, ["laws/loop.py", "laws/refunds.py"], functions=["shop.loop:countdown"]
    )
    ids = [b.id for b in m.bugs]
    assert ids and all(i.startswith("src/shop/loop.py::countdown#") for i in ids)
    assert m.laws_by_function["shop.loop:countdown"] == ["loop.countdown_steps"]
    caught = set(m.caught["loop.countdown_steps"])
    deleted = next(b for b in m.bugs if b.description.startswith("deleted `n -= 1`"))
    assert deleted.id in caught  # an endless loop counts as caught (it timed out)
    assert m.caught["refunds.refund_is_capped"] == []  # it never runs countdown
    # same source -> same ids
    m2 = catch_matrix(root, cfg, ["laws/loop.py"], functions=["shop.loop:countdown"], max_bugs=2)
    assert [b.id for b in m2.bugs] == [ids[0], ids[-1]]


def test_collection_error_is_a_note_not_a_crash(tmp_path):
    root = make_project(tmp_path, {"laws/broken.py": "def oops(:\n"})
    report = run_strength(root, config.load_config(root))
    assert any("laws/broken.py could not be loaded" in n for n in report.notes)
    assert {r.law_id for r in report.laws} == {
        "refunds.refund_is_capped",
        "fees.fee_is_two_percent",
    }


def test_no_laws_yet(tmp_path):
    root = make_project(tmp_path)
    shutil.rmtree(root / "laws")
    report = run_strength(root, config.load_config(root))
    assert report.exit_code == 0 and report.laws == []
    assert report.summary.startswith("NO LAWS YET")
    assert "shop.refunds:refund" in report.uncovered_functions


@pytest.mark.parametrize("full", [False, True])
def test_strength_cli_json(tmp_path, capsys, full):
    import json

    from lawkeep import cli

    root = make_project(tmp_path)
    argv = ["strength", "--json", "--root", str(root)] + (["--full"] if full else [])
    code = cli.main(argv)
    data = json.loads(capsys.readouterr().out)
    assert code == 1 == data["exit_code"]
    assert data["kind"] == "strength" and data["mode"] == ("full" if full else "quick")
    assert {law["law_id"] for law in data["laws"]} == {
        "refunds.refund_is_capped",
        "fees.fee_is_two_percent",
    }


@pytest.mark.parametrize("example", ["billing", "bookings"])
def test_examples_have_no_weak_laws(tmp_path, example):
    """The showcase examples pin down every behaviour: no law is flagged as weak."""
    import shutil as _shutil

    from lawkeep import config as _config
    from lawkeep.strength import run_strength

    root = tmp_path / example
    _shutil.copytree(Path(__file__).parent.parent / "examples" / example, root)
    report = run_strength(root, _config.load_config(root))
    assert report.laws and len(report.laws) == 6
    assert [r.law_id for r in report.laws if r.flags] == [], [
        (r.law_id, r.flags) for r in report.laws if r.flags
    ]
