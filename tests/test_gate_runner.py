"""Gate (B): running laws in a clean subprocess (made-up refunds project)."""

from __future__ import annotations

import importlib.util
import shutil
import textwrap
from pathlib import Path

import pytest

from lawkeep import config, history, ledger, runner
from lawkeep.errors import SetupError

FIXTURES = Path(__file__).parent / "fixtures" / "gate"


def make_project(tmp_path: Path, *, init: bool = True) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(FIXTURES / "refunds_project", root)
    (root / "laws" / "history.jsonl").touch()
    (root / "laws" / "failing_examples.jsonl").touch()
    if init:
        history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def plant_bug(root: Path) -> None:
    shutil.copy(FIXTURES / "buggy_refunds.py", root / "src" / "shop" / "refunds.py")


def run(root: Path, **kw) -> runner.RunOutcome:
    return runner.run_laws(root, config.load_config(root), **kw)


def by_id(outcome: runner.RunOutcome) -> dict:
    return {r.law_id: r for r in outcome.results}


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


def test_all_laws_hold(tmp_path):
    root = make_project(tmp_path)
    out = run(root)
    res = by_id(out)
    assert set(res) == {
        "refunds.refund_is_capped",
        "refunds.refund_never_negative",
        "refunds.fee_example",
    }
    assert all(r.status == "holds" for r in res.values()), out.stdout + out.stderr
    capped = res["refunds.refund_is_capped"]
    assert capped.level == "L2" and capped.examples_tried == 60  # lawkeep.toml [check.pr]
    assert capped.evidence["derandomize"] is True and capped.evidence["max_examples"] == 60
    assert capped.sentence == "A refund never exceeds the original charge"
    assert capped.file == "laws/refunds.py"
    assert res["refunds.fee_example"].level == "L1"
    assert out.new_failures == []


def test_planted_bug_fails_with_shrunk_example(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    out = run(root)
    res = by_id(out)
    capped = res["refunds.refund_is_capped"]
    assert capped.status == "fails"
    assert capped.simplest_failing_example == {"charge": "0", "amount": "1001"}
    assert "AssertionError" in capped.failure_message
    assert res["refunds.refund_never_negative"].status == "holds"
    assert len(out.new_failures) == 1 and out.new_failures[0]["law_id"] == capped.law_id
    # the runner never writes the ledger itself
    assert ledger.load(root) == []


def test_same_code_same_verdict(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    a = by_id(run(root))["refunds.refund_is_capped"]
    b = by_id(run(root))["refunds.refund_is_capped"]
    assert a.simplest_failing_example == b.simplest_failing_example
    assert a.evidence["seed"] == b.evidence["seed"]


def test_saved_failing_example_is_replayed(tmp_path):
    """A bug at one exact input that random search will not find is caught by replay."""
    from hypothesis.database import choices_to_bytes

    root = make_project(tmp_path)
    write(
        root,
        "src/shop/refunds.py",
        """
        def refund(charge, amount):
            if amount < 0:
                raise ValueError("negative")
            if charge == 4321 and amount == 98765:
                return amount  # special-cased bug
            return min(amount, charge)

        def fee(amount):
            return amount * 2 // 100
        """,
    )
    assert by_id(run(root))["refunds.refund_is_capped"].status == "holds"
    ledger.add(
        root,
        "refunds.refund_is_capped",
        {"charge": "4321", "amount": "98765"},
        choices=choices_to_bytes((4321, 98765)),
    )
    capped = by_id(run(root))["refunds.refund_is_capped"]
    assert capped.status == "fails"
    assert capped.evidence["saved_failures_replayed"] == 1
    assert capped.simplest_failing_example == {"charge": "4321", "amount": "98765"}


def test_outside_conftest_is_not_loaded(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    # a conftest.py at the root that would "fix" refund for every pytest run
    write(
        root,
        "conftest.py",
        """
        import shop.refunds as r
        r.refund = lambda charge, amount: min(amount, charge)
        """,
    )
    write(root, "src/conftest.py", "raise SystemExit('must not be imported')\n")
    capped = by_id(run(root))["refunds.refund_is_capped"]
    assert capped.status == "fails"


def test_sitecustomize_and_pth_in_project_are_not_loaded(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    trick = "import shop.refunds as r\nr.refund = lambda c, a: min(a, c)\n"
    write(root, "sitecustomize.py", trick)
    write(root, "src/sitecustomize.py", trick)
    write(root, "src/usercustomize.py", trick)
    assert by_id(run(root))["refunds.refund_is_capped"].status == "fails"


def test_project_pytest_settings_do_not_apply(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    write(
        root,
        "pytest.ini",
        "[pytest]\naddopts = -p no:lawkeep.pytest_plugin --deselect laws/refunds.py::refund_is_capped\n",
    )
    assert by_id(run(root))["refunds.refund_is_capped"].status == "fails"


def test_laws_conftest_fixtures_load_but_hooks_do_not(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    write(
        root,
        "laws/conftest.py",
        """
        import pytest

        @pytest.fixture
        def big_charge():
            return 5000

        def pytest_runtest_makereport(item, call):
            raise SystemExit("hooks in laws/conftest.py must be ignored")

        def pytest_collection_modifyitems(items):
            items.clear()
        """,
    )
    write(
        root,
        "laws/fixture_law.py",
        """
        from lawkeep import law
        from shop.refunds import refund

        @law("A small refund of a big charge is paid in full")
        def small_refund_paid(big_charge):
            assert refund(big_charge, 10) == 10
        """,
    )
    out = run(root)
    res = by_id(out)
    assert res["fixture_law.small_refund_paid"].status == "holds"
    assert res["refunds.refund_is_capped"].status == "fails"
    assert any("ignores hooks" in n for n in out.notes)


def test_tests_without_law_are_ignored_with_a_note(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/extra.py",
        """
        def test_not_a_law():
            raise AssertionError("must not run")
        """,
    )
    out = run(root)
    assert "extra.test_not_a_law" not in by_id(out)
    assert out.ignored == ["laws/extra.py::test_not_a_law"]
    assert any("no @law sentence" in n for n in out.notes)
    assert all(r.status == "holds" for r in out.results)


def test_any_name_is_collected_and_xfail_does_not_hide_failure(tmp_path):
    root = make_project(tmp_path)
    plant_bug(root)
    write(
        root,
        "laws/more.py",
        """
        import pytest
        from lawkeep import law, given, st
        from shop.refunds import refund

        @pytest.mark.xfail(reason="agent says flaky")
        @law("Refunding the whole charge returns the whole charge")
        @given(charge=st.integers(min_value=0, max_value=5000))
        def whole(charge):
            assert refund(charge, charge + 2000) == charge

        @pytest.mark.skip(reason="agent skipped it")
        @law("A skipped law")
        def skipped_law():
            assert False
        """,
    )
    res = by_id(run(root))
    assert res["more.whole"].status == "fails"
    assert "xfail" in res["more.whole"].failure_message
    assert res["more.skipped_law"].status == "skipped"
    assert res["more.skipped_law"].failure_message == "agent skipped it"


def test_fewer_examples_in_law_has_no_effect(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/few.py",
        """
        from hypothesis import settings
        from lawkeep import law, given, st
        from shop.refunds import refund

        @law("Refund of zero is zero")
        @settings(max_examples=1)
        @given(charge=st.integers(min_value=0, max_value=10**6))
        def zero(charge):
            assert refund(charge, 0) == 0

        @law("Asks for more")
        @settings(max_examples=150)
        @given(charge=st.integers(min_value=0, max_value=10**6))
        def more(charge):
            assert refund(charge, 0) == 0
        """,
    )
    res = by_id(run(root))
    assert res["few.zero"].examples_tried == 60
    assert res["few.more"].examples_tried == 150


def test_state_machine_law(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/ledger_machine.py",
        """
        from hypothesis.stateful import RuleBasedStateMachine, rule, invariant
        from lawkeep import law, st
        from shop.refunds import refund

        @law("Refunds never exceed what was charged so far")
        class Payments(RuleBasedStateMachine):
            def __init__(self):
                super().__init__()
                self.charged = 0
                self.refunded = 0

            @rule(n=st.integers(min_value=0, max_value=100))
            def charge(self, n):
                self.charged += n

            @rule(n=st.integers(min_value=0, max_value=100))
            def give_back(self, n):
                self.refunded += refund(self.charged - self.refunded, n)

            @invariant()
            def capped(self):
                assert self.refunded <= self.charged
        """,
    )
    res = by_id(run(root))
    sm = res["ledger_machine.Payments"]
    assert sm.status == "holds" and sm.level == "L2", sm.failure_message
    assert sm.examples_tried > 0


def test_collection_error_marks_its_laws_as_error(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/broken.py",
        """
        from lawkeep import law
        from shop.does_not_exist import nothing

        @law("This law cannot even be imported")
        def broken():
            assert nothing()
        """,
    )
    out = run(root)
    res = by_id(out)
    assert res["broken.broken"].status == "error"
    assert "could not be loaded" in res["broken.broken"].failure_message
    assert out.collection_errors
    assert res["refunds.refund_is_capped"].status == "holds"


def test_unsatisfiable_law_is_an_error_not_a_failure(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/vacuous.py",
        """
        from lawkeep import law, given, st, assume

        @law("Never satisfiable")
        @given(x=st.integers())
        def nothing_runs(x):
            assume(False)
        """,
    )
    r = by_id(run(root))["vacuous.nothing_runs"]
    assert r.status == "error"
    assert "could not run properly" in r.failure_message


def test_law_ids_filter(tmp_path):
    root = make_project(tmp_path)
    out = run(root, law_ids=["refunds.fee_example"])
    assert [r.law_id for r in out.results] == ["refunds.fee_example"]


def test_unknown_law_id_is_a_setup_problem(tmp_path):
    """A mistyped --law must not turn into 'ALL LAWS HOLD (nothing checked)'."""
    from lawkeep.errors import SetupError

    root = make_project(tmp_path)
    with pytest.raises(SetupError, match="nope.law"):
        run(root, law_ids=["refunds.fee_example", "nope.law"])


def test_no_laws_yet(tmp_path):
    root = tmp_path / "empty"
    (root / "laws").mkdir(parents=True)
    out = run(root)
    assert out.results == [] and "no law files" in out.notes[0]


def test_patched_bytecode_is_ignored(tmp_path):
    """A __pycache__ .pyc whose header matches the source but whose code is buggy."""
    root = make_project(tmp_path)
    src = root / "src" / "shop" / "refunds.py"
    buggy_code = compile((FIXTURES / "buggy_refunds.py").read_text(), str(src), "exec")
    st_ = src.stat()
    from importlib._bootstrap_external import (
        _code_to_timestamp_pyc,  # pyright: ignore[reportAttributeAccessIssue]  # private CPython helper, not in typeshed
    )

    pyc = Path(importlib.util.cache_from_source(str(src)))
    pyc.parent.mkdir(exist_ok=True)
    pyc.write_bytes(_code_to_timestamp_pyc(buggy_code, int(st_.st_mtime), st_.st_size))
    assert by_id(run(root))["refunds.refund_is_capped"].status == "holds"
    # and no bytecode is written by the gate
    assert sorted(p.name for p in pyc.parent.iterdir()) == [pyc.name]


def test_profile_object_and_deep_profile(tmp_path):
    root = make_project(tmp_path)
    cfg = config.load_config(root)
    out = runner.run_pytest(root, cfg, ["laws/refunds.py"], profile="deep")
    capped = {r.law_id: r for r in out.results}["refunds.refund_is_capped"]
    assert capped.evidence["derandomize"] is False and capped.examples_tried == 200
    assert capped.evidence["seed"] is not None  # random seed recorded for reproduction


def test_missing_target_is_a_setup_error(tmp_path):
    root = make_project(tmp_path)
    with pytest.raises(SetupError):
        runner.run_pytest(root, config.load_config(root), ["laws/nope.py"])


def test_crosshair_missing_is_a_setup_error(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    cfg = config.load_config(root)
    monkeypatch.setattr(runner, "crosshair_available", lambda: False)
    import dataclasses

    prof = dataclasses.replace(cfg.check.get_profile(), backend="crosshair")
    with pytest.raises(SetupError) as e:
        runner.run_pytest(root, cfg, ["laws/refunds.py"], profile=prof)
    assert "lawkeep[deep]" in (e.value.hint or "")


def test_timeout_returns_outcome_with_errors(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/slow.py",
        """
        import time
        from lawkeep import law

        @law("Never finishes")
        def stuck():
            time.sleep(60)
        """,
    )
    out = run(root, timeout=3)
    assert out.timed_out
    assert all(r.status == "error" for r in out.results)


def test_extra_plugins_and_sys_path_are_passed(tmp_path):
    """run_pytest accepts extra plugin modules and the folders they live in."""
    root = make_project(tmp_path)
    plugdir = tmp_path / "plugins"
    write(
        plugdir,
        "lawkeep_test_swap.py",
        """
        def pytest_configure(config):
            import shop.refunds as r
            r.refund = lambda charge, amount: amount  # a dumb version
        """,
    )
    out = runner.run_pytest(
        root,
        config.load_config(root),
        ["laws/refunds.py"],
        extra_plugins=["lawkeep_test_swap"],
        extra_sys_path=[str(plugdir)],
    )
    assert {r.law_id: r for r in out.results}["refunds.refund_is_capped"].status == "fails"


@pytest.mark.skipif(not runner.crosshair_available(), reason="hypothesis-crosshair not installed")
def test_crosshair_backend_reports_l3(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/tiny.py",
        """
        from lawkeep import law, given, st
        from shop.refunds import fee

        @law("The fee is never negative")
        @given(amount=st.integers(min_value=0, max_value=1000))
        def fee_not_negative(amount):
            assert fee(amount) >= 0
        """,
    )
    import dataclasses

    cfg = config.load_config(root)
    prof = dataclasses.replace(cfg.check.get_profile(), backend="crosshair", examples=20)
    out = runner.run_pytest(root, cfg, ["laws/tiny.py"], profile=prof, timeout=300)
    r = {r.law_id: r for r in out.results}["tiny.fee_not_negative"]
    assert r.level == "L3", out.stdout + out.stderr
    assert r.status == "holds", r.failure_message
    assert "exhausted" in r.evidence and r.evidence["bounded"] == (not r.evidence["exhausted"])
