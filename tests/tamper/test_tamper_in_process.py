"""Cheating tests, part 2: tricks played by the CODE UNDER TEST from inside the law run.

The gate runs the project's code in the same Python process as the laws. lawkeep cannot make
that impossible (the code under test is trusted, see the README's "Known limits"), so for each
trick the test checks that it is either defeated (the planted bug still fails the gate) or
flagged loudly (an integrity issue that makes the gate wait for a person, result code 2).
Never a silent "ALL LAWS HOLD".

Each test copies examples/billing into a temp git repo, applies the planted bug, adds the
trick to the code, and runs the real `lawkeep check --json` in a fresh process. No network, no
coding agent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
_SCRUB = ("PYTHONPATH", "LAWKEEP_ROOT", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP")
REFUND_LAWS = (
    "refunds.refund_never_exceeds_payment",
    "refunds.full_refund_gives_back_exact_payment",
    "refunds.partial_refund_is_exact_and_fee_share_is_fair",
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(extra)
    return env


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Tamper Test",
            "-c",
            "user.email=tamper@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def make_buggy_project(tmp_path: Path) -> Path:
    root = (tmp_path / "billing").resolve()
    shutil.copytree(
        EXAMPLES / "billing",
        root,
        ignore=shutil.ignore_patterns("__pycache__", ".hypothesis", ".lawkeep", ".pytest_cache"),
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    _git(root, "apply", "planted_bug.patch")
    return root


def check(root: Path, **env: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", "check", "--json", "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=900,
        env=_env(**env),
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"no JSON from lawkeep check:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    assert report["exit_code"] == proc.returncode
    return proc.returncode, report


def statuses(report: dict) -> dict[str, str]:
    return {r["law_id"]: r["status"] for r in report["laws"]}


def source_flags(report: dict, path: str) -> list[dict]:
    return [
        i for i in report["integrity"] if i["kind"] == "source_side_door" and i.get("path") == path
    ]


def add_code(root: Path, rel: str, code: str) -> None:
    p = root / rel
    p.write_text(p.read_text(encoding="utf-8") + "\n" + code, encoding="utf-8")


def assert_not_silent(code: int, report: dict) -> None:
    assert code != 0, f"the trick produced a silent ALL LAWS HOLD: {report['summary']}"


# ---------------------------------------------------------------------------------------------


def test_trick_atexit_hook_rewrites_the_results_file(tmp_path):
    root = make_buggy_project(tmp_path)
    add_code(
        root,
        "src/billing/__init__.py",
        """
import atexit as _ax, json as _js, os as _os

def _forge():
    for _k in ("LAWKEEP_RESULTS_FILE",):
        _p = _os.environ.get(_k)
        if _p and _os.path.exists(_p):
            _d = _js.load(open(_p))
            for _r in _d["results"]:
                _r["status"] = "holds"
            _d["new_failures"] = []
            _js.dump(_d, open(_p, "w"))

_ax.register(_forge)
""",
    )
    code, report = check(root)
    assert code == 1, report["summary"]  # the results never pass through a file it can reach
    for law_id in REFUND_LAWS:
        assert statuses(report)[law_id] in ("fails", "error")
    flags = source_flags(report, "src/billing/__init__.py")
    assert flags and flags[0]["severity"] == "error"  # mentions LAWKEEP_RESULTS_FILE


def test_trick_code_detects_the_gate_through_its_environment(tmp_path):
    root = make_buggy_project(tmp_path)
    # Undo the planted bug only while the gate runs (production keeps it).
    add_code(
        root,
        "src/billing/refunds.py",
        """
import os as _os
if _os.environ.get("LAWKEEP_RESULTS_FILE") or _os.environ.get("LAWKEEP_RESULTS_FD"):
    _buggy = refund
    def refund(charge, amount):
        r = _buggy(charge, amount)
        return Refund(r.charge_id, r.total - r.fee_returned, r.fee_returned)
""",
    )
    code, report = check(root)
    assert code == 1, report["summary"]  # the variables are gone before project code runs
    assert source_flags(report, "src/billing/refunds.py")


def test_trick_code_patches_hypothesis_to_swallow_failures(tmp_path):
    root = make_buggy_project(tmp_path)
    add_code(
        root,
        "src/billing/__init__.py",
        """
try:
    import hypothesis.core as _hc
    _orig = _hc.StateForActualGivenExecution.execute_once
    def _quiet(self, *a, **k):
        try:
            return _orig(self, *a, **k)
        except AssertionError:
            return None
    _hc.StateForActualGivenExecution.execute_once = _quiet
except Exception:
    pass
""",
    )
    code, report = check(root)
    assert_not_silent(code, report)
    flags = source_flags(report, "src/billing/__init__.py")
    assert flags and flags[0]["severity"] == "error", report["integrity"]
    assert "hypothesis" in flags[0]["message"]


@pytest.mark.parametrize(
    "raise_skip",
    [
        "import unittest; raise unittest.SkipTest('flaky payment backend')",
        "import pytest; pytest.skip('not today')",
    ],
)
def test_trick_code_raises_a_skip_on_bad_inputs(tmp_path, raise_skip):
    root = make_buggy_project(tmp_path)
    p = root / "src/billing/refunds.py"
    text = p.read_text(encoding="utf-8")
    old = "    return Refund(charge_id=charge.id, total=amount + fee_returned, fee_returned=fee_returned)"
    assert old in text
    p.write_text(
        text.replace(old, f"    if fee_returned:\n        {raise_skip}\n{old}"), encoding="utf-8"
    )
    code, report = check(root)
    assert code == 1, report["summary"]
    st = statuses(report)
    assert "skipped" not in st.values()
    assert st["refunds.refund_never_exceeds_payment"] == "error"
    assert source_flags(report, "src/billing/refunds.py")


def test_trick_code_calls_assume_on_the_inputs_it_gets_wrong(tmp_path):
    root = make_buggy_project(tmp_path)
    p = root / "src/billing/refunds.py"
    text = p.read_text(encoding="utf-8")
    old = "    return Refund(charge_id=charge.id, total=amount + fee_returned, fee_returned=fee_returned)"
    p.write_text(
        text.replace(
            old,
            f"    if fee_returned:\n        from hypothesis import assume; assume(False)\n{old}",
        ),
        encoding="utf-8",
    )
    code, report = check(root)
    assert code == 1, report["summary"]
    errs = [r for r in report["laws"] if r["status"] == "error"]
    assert any("outside laws/" in (r["failure_message"] or "") for r in errs), report["laws"]


def test_trick_values_that_compare_equal_to_everything_are_flagged(tmp_path):
    root = make_buggy_project(tmp_path)
    add_code(
        root,
        "src/billing/refunds.py",
        """
class _Amt(Decimal):
    def __eq__(self, other):
        return True
    def __le__(self, other):
        return True
    def __ge__(self, other):
        return True
    __hash__ = Decimal.__hash__
""",
    )
    code, report = check(root)
    assert_not_silent(code, report)
    flags = source_flags(report, "src/billing/refunds.py")
    assert flags and "always returns True" in flags[0]["message"]


def test_trick_extra_sys_path_from_the_shell_is_ignored(tmp_path):
    root = make_buggy_project(tmp_path)
    clean = tmp_path / "clean"
    shutil.copytree(EXAMPLES / "billing" / "src", clean)
    code, report = check(root, LAWKEEP_EXTRA_SYS_PATH=str(clean))
    assert code == 1, report["summary"]


def test_trick_check_with_one_law_says_it_is_a_partial_run(tmp_path):
    root = make_buggy_project(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "lawkeep",
            "check",
            "--json",
            "--root",
            str(root),
            "--law",
            "discounts.discount_stays_between_zero_and_price",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    report = json.loads(proc.stdout)
    assert "PARTIAL RUN" in report["summary"] and "5 other law(s) were not run" in report["summary"]
    assert report["only_laws"] == ["discounts.discount_stays_between_zero_and_price"]
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", "check", "--json", "--root", str(root), "--law", "typo"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    assert proc.returncode == 3, proc.stdout
