"""Proof that the two example projects do what the docs say.

- The examples' laws.lock and law history are consistent (no unrecorded change, nothing
  rewritten by hand). Runs today.
- Their laws hold on today's code and fail on the planted bug, run directly through Hypothesis
  without lawkeep's gate. Runs today; this checks the examples, not the gate.
- Through the real gate: each example passes ``lawkeep check``, and each planted bug fails it
  with a small failing input. xfail while ``lawkeep check`` is still a stub.

Every test works on a throwaway copy in a temp folder; nothing touches examples/ itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lawkeep import discovery, history, lock

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"

PLANTED = {
    # example -> laws the planted bug must break, and a law it must leave alone
    "billing": (
        {
            "refunds.full_refund_gives_back_exact_payment",
            "refunds.partial_refund_is_exact_and_fee_share_is_fair",
        },
        "rounding.rounding_is_to_the_nearest_cent",
    ),
    "bookings": (
        {"booking_rules.bookings_never_overlap"},
        "booking_rules.cancelling_frees_the_slot",
    ),
}

_SCRUB = ("PYTHONPATH", "LAWKEEP_ROOT", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(extra)
    return env


def copy_example(tmp_path: Path, name: str) -> Path:
    root = (tmp_path / name).resolve()
    shutil.copytree(
        EXAMPLES / name,
        root,
        ignore=shutil.ignore_patterns("__pycache__", ".hypothesis", ".lawkeep", ".pytest_cache"),
    )
    return root


def apply_patch(root: Path, name: str = "planted_bug.patch") -> None:
    subprocess.run(
        ["git", "apply", "--unsafe-paths", name], cwd=root, check=True, capture_output=True
    )


# Runs every law of a project directly with Hypothesis (no lawkeep gate, no pytest) and prints
# one JSON line per law. Used only to check the example projects themselves.
DIRECT_RUNNER = textwrap.dedent(
    """
    import importlib.util, json, sys
    from pathlib import Path
    from hypothesis import settings
    from lawkeep import get_law
    from lawkeep.discovery import iter_python_files
    root = Path.cwd()
    for f in iter_python_files(root):
        spec = importlib.util.spec_from_file_location("law_" + f.stem, f)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for obj in list(vars(m).values()):
            meta = get_law(obj)
            if not meta or getattr(obj, "__module__", None) != m.__name__:
                continue
            try:
                settings(max_examples=200, derandomize=True, database=None)(obj)()
                status = "holds"
            except Exception as exc:
                status = "fails"
            print(json.dumps({"law_id": meta.law_id, "status": status}))
    """
)


def run_directly(root: Path) -> dict[str, str]:
    proc = subprocess.run(
        [sys.executable, "-c", DIRECT_RUNNER],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(PYTHONPATH=str(root / "src"), LAWKEEP_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1"),
    )
    assert proc.returncode == 0, proc.stderr
    rows = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    return {r["law_id"]: r["status"] for r in rows}


def run_check(root: Path) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", "check", "--json", "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=900,
        env=_env(),
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"no JSON from lawkeep check:\n{proc.stdout}\n{proc.stderr}")
    if (report.get("data") or {}).get("not_implemented"):
        pytest.xfail("`lawkeep check` is not built yet (integration pending)")
    assert report["exit_code"] == proc.returncode
    return proc.returncode, report


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_example_lock_and_history_are_consistent(name):
    root = (EXAMPLES / name).resolve()
    the_lock = lock.read_lock(root)
    assert the_lock is not None
    assert lock.diff_against_lock(root).is_empty, (
        f"examples/{name} changed since laws.lock was written; rebuild it with "
        f"`uv run python examples/make_example_history.py examples/{name}`"
    )
    assert lock.check_append_only(root, the_lock) == []
    entries = history.read(root)
    assert history.unexplained_lock_paths(the_lock, entries) == []
    assert history.pending(entries) == []
    found = discovery.discover_laws(root)
    assert found.problems == []
    assert sorted(the_lock.laws) == sorted(found.by_id())
    assert 4 <= len(found.laws) <= 6
    for law in found.laws:
        assert law.sentence and law.sentence[0].isupper()
    added = {e.law_id for e in entries if e.action == "add_law" and e.status == "applied"}
    assert added == set(found.by_id()), "every law needs an add_law entry in the history"
    # every law was approved by a person, or (companion laws added with `lawkeep propose`)
    # went in automatically because it only adds checks
    assert all(
        e.approved_by or (e.permission_used == "auto" and e.direction == "stricter")
        for e in entries
        if e.action == "add_law"
    )


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_example_patches_apply_cleanly(tmp_path, name):
    root = copy_example(tmp_path, name)
    for patch in sorted(root.glob("*.patch")):
        subprocess.run(
            ["git", "apply", "--check", patch.name], cwd=root, check=True, capture_output=True
        )


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_example_laws_hold_and_planted_bug_breaks_them_when_run_directly(tmp_path, name):
    root = copy_example(tmp_path, name)
    before = run_directly(root)
    assert before and set(before.values()) == {"holds"}, before
    apply_patch(root)
    after = run_directly(root)
    must_fail, unaffected = PLANTED[name]
    for law_id in must_fail:
        assert after[law_id] == "fails", after
    assert after[unaffected] == "holds"


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_example_passes_lawkeep_check(tmp_path, name):
    root = copy_example(tmp_path, name)
    code, report = run_check(root)
    assert code == 0, report.get("summary")
    assert report["summary"].startswith("ALL LAWS HOLD")
    locked = lock.read_lock(root)
    assert locked is not None
    assert {r["law_id"] for r in report["laws"]} == set(locked.laws)
    for r in report["laws"]:
        assert r["status"] == "holds"
        assert r["level"] in ("L2", "L3")
        assert r["examples_tried"] > 1
    assert [i for i in report["integrity"] if i["severity"] == "error"] == []


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_planted_bug_fails_the_gate_with_a_minimal_failing_input(tmp_path, name):
    root = copy_example(tmp_path, name)
    apply_patch(root)
    code, report = run_check(root)
    assert code == 1
    assert report["summary"].startswith("SOME LAWS FAIL")
    must_fail, unaffected = PLANTED[name]
    by_id = {r["law_id"]: r for r in report["laws"]}
    for law_id in must_fail:
        r = by_id[law_id]
        assert r["status"] == "fails"
        example = r["simplest_failing_example"]
        assert example, f"{law_id} failed without a simplest failing example"
        # Shrunk inputs are short: a small charge / two short bookings, not a wall of data.
        assert len(json.dumps(example)) < 600, example
    assert by_id[unaffected]["status"] == "holds"
