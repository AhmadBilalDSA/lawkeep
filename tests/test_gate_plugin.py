"""Gate (B): pytest plugin helpers, and that the plugin stays passive in normal pytest runs."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from lawkeep import pytest_plugin as pp


def test_parse_multiline_example():
    note = "Failing test case: refund_is_capped(\n    charge=0,\n    amount=1001,\n)"
    assert pp.parse_falsifying_example(note) == {"charge": "0", "amount": "1001"}


def test_parse_old_style_and_comment():
    note = "Falsifying example: t(\n    a=10,\n    b='',  # or any other generated value\n)"
    assert pp.parse_falsifying_example(note) == {"a": "10", "b": "''"}


def test_parse_nested_value_keeps_inner_lines():
    note = "Failing test case: t(\n    order=Order(\n        total=5,\n    ),\n    n=1,\n)"
    ex = pp.parse_falsifying_example(note)
    assert ex == {"order": "Order(\n    total=5,\n)", "n": "1"}


def test_parse_no_args_and_state_machine_and_other_notes():
    assert pp.parse_falsifying_example("Failing test case: t()") == {}
    sm = "Falsifying example:\nstate = Machine()\nstate.add(x=1)\nstate.teardown()"
    assert pp.parse_falsifying_example(sm) == {
        "call": "state = Machine()\nstate.add(x=1)\nstate.teardown()"
    }
    assert pp.parse_falsifying_example("Draw 1: 5") is None


def test_examples_from_exception_reads_notes_and_draws():
    exc = AssertionError("boom")
    notes = ["Failing test case: t(\n    data=data(...),\n)", "Draw 1: 42"]
    exc.__notes__ = notes  # pyright: ignore[reportAttributeAccessIssue]  # typed only on 3.11+
    assert pp.examples_from_exception(exc) == [{"data": "data(...)", "draw 1": "42"}]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup needs Python 3.11")
def test_failure_text_and_groups():
    assert pp.failure_text(AssertionError("assert 1 <= 0")) == "AssertionError: assert 1 <= 0"
    if sys.version_info >= (3, 11):  # also tells the type checker that ExceptionGroup exists
        group = ExceptionGroup("many", [ValueError("a"), KeyError("b")])  # noqa: F821
        text = pp.failure_text(group)
        assert text.startswith("2 distinct failures") and "ValueError: a" in text


def test_is_setup_error():
    from hypothesis.errors import FailedHealthCheck, Unsatisfiable

    assert pp.is_setup_error(Unsatisfiable("x"))
    assert pp.is_setup_error(FailedHealthCheck("x"))
    assert not pp.is_setup_error(AssertionError("x"))
    assert not pp.is_setup_error(ValueError("x"))


def test_stable_seed_is_stable_and_differs_per_law():
    assert pp.stable_seed("a.law") == pp.stable_seed("a.law")
    assert pp.stable_seed("a.law") != pp.stable_seed("b.law")


def test_plugin_is_not_active_in_this_pytest_run():
    # this repo's own pytest run loads the plugin through the pytest11 entry point; only
    # run_check (the law-running child) switches it on
    assert pp._active is None  # pyright: ignore[reportPrivateUsage]  # the switch itself is under test


def test_plugin_is_passive_in_a_normal_pytest_run(tmp_path):
    """With autoload on (as in any project) the plugin only registers the marker."""
    (tmp_path / "test_normal.py").write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.mark.law("a sentence")
            def test_marked():
                assert True

            def test_plain():
                assert True

            def not_a_test_name():
                raise AssertionError("must not be collected")
            """
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAWKEEP_")}
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--strict-markers",
            str(tmp_path / "test_normal.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout
    assert not list(tmp_path.glob("*.json"))


def test_environment_cannot_switch_the_plugin_on(tmp_path):
    """The old switch variables do nothing now: a normal pytest run stays passive."""
    (tmp_path / "test_normal.py").write_text("def test_plain():\n    assert True\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAWKEEP_")}
    env["LAWKEEP_RESULTS_FILE"] = str(tmp_path / "results.json")
    env["LAWKEEP_RESULTS_FD"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_normal.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout and "{" not in proc.stdout
    assert not (tmp_path / "results.json").exists()
