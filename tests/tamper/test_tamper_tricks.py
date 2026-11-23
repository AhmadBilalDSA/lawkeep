"""The cheating test suite: each known trick, scripted against a throwaway copy of an example.

Every test is named after the trick it plays. Each one:

1. copies ``examples/billing`` into a temp folder and makes a git commit of it (the "before"),
2. plays the trick the way a coding agent might (usually on top of the planted bug, so the
   trick would hide a real bug),
3. where it helps, shows that the trick really works on plain Python / pytest (so the test is
   not checking a harmless change), and checks the foundation-level evidence lawkeep relies on,
4. runs the real ``lawkeep check --json`` (or ``report``) in a fresh process and asserts that
   lawkeep catches it and says so (an integrity issue, a failing law, or code marked as not
   protected).

While ``lawkeep check`` / ``report`` are still stubs, the tests stop at step 4 with an
``xfail`` that says "not built yet"; steps 1 to 3 still run. No test calls a coding agent or
the network. Kinds and fields asserted here come from docs/ARCHITECTURE.md, section 4.6.
"""

from __future__ import annotations

import importlib.util
import json
import marshal
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from lawkeep import history, lock

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"

REFUND_LAWS = (
    "refunds.refund_never_exceeds_payment",
    "refunds.full_refund_gives_back_exact_payment",
    "refunds.partial_refund_is_exact_and_fee_share_is_fair",
)
FULL_REFUND_LAW = "refunds.full_refund_gives_back_exact_payment"

# Environment variables that must not leak from the test run into the processes we start.
_SCRUB = ("PYTHONPATH", "LAWKEEP_ROOT", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONSTARTUP")


# ---------------------------------------------------------------------------------------------
# helpers


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(extra)
    return env


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(
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
    return out.stdout.strip()


def make_project(tmp_path: Path, name: str = "billing") -> Path:
    """A throwaway copy of an example project, committed to a fresh git repo."""
    root = (tmp_path / name).resolve()
    shutil.copytree(
        EXAMPLES / name,
        root,
        ignore=shutil.ignore_patterns("__pycache__", ".hypothesis", ".lawkeep", ".pytest_cache"),
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    assert lock.diff_against_lock(root).is_empty, "the example's laws.lock is out of date"
    return root


def apply_patch(root: Path, patch_name: str) -> None:
    _git(root, "apply", patch_name)


def run_lawkeep(root: Path, command: str, *args: str) -> tuple[int, dict]:
    """Run ``python -m lawkeep <command> --json --root <root>`` in a fresh process."""
    proc = subprocess.run(
        [sys.executable, "-m", "lawkeep", command, "--json", "--root", str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=900,
        env=_env(),
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"`lawkeep {command} --json` did not print one JSON document.\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        )
    assert report.get("exit_code") == proc.returncode, (
        f"JSON exit_code {report.get('exit_code')} differs from the process result "
        f"{proc.returncode}"
    )
    return proc.returncode, report


def not_built(report: dict) -> bool:
    return bool((report.get("data") or {}).get("not_implemented"))


def gate(root: Path, command: str = "check", *args: str) -> tuple[int, dict]:
    """Run a lawkeep command; xfail (not fail) while the command is still a stub."""
    code, report = run_lawkeep(root, command, *args)
    if not_built(report):
        pytest.xfail(f"`lawkeep {command}` is not built yet (integration pending)")
    assert code != 3, f"`lawkeep {command}` hit a setup problem: {report}"
    return code, report


def issues(report: dict, kind: str | None = None, path: str | None = None) -> list[dict]:
    found = report.get("integrity") or []
    return [
        i
        for i in found
        if (kind is None or i.get("kind") == kind) and (path is None or i.get("path") == path)
    ]


def assert_issue(report: dict, kinds: str | tuple[str, ...], path: str, severity: str | None):
    kinds = (kinds,) if isinstance(kinds, str) else kinds
    hits = [i for i in issues(report, path=path) if i.get("kind") in kinds]
    assert hits, (
        f"expected an integrity issue of kind {kinds} for {path!r}; lawkeep reported: "
        f"{json.dumps(report.get('integrity'), indent=1)}"
    )
    if severity:
        assert any(i.get("severity") == severity for i in hits), hits
    for i in hits:
        assert i.get("message"), "every integrity issue needs a plain-English message"
    return hits


def law_result(report: dict, law_id: str) -> dict:
    for r in report.get("laws") or []:
        if r.get("law_id") == law_id:
            return r
    ids = [r.get("law_id") for r in report.get("laws") or []]
    pytest.fail(f"law {law_id!r} missing from the check report (got {ids})")


def assert_refund_laws_not_passing(report: dict) -> None:
    """With the planted bug in place, the refund laws must not report "holds"."""
    for law_id in REFUND_LAWS:
        status = law_result(report, law_id)["status"]
        assert status != "holds", f"{law_id} reported 'holds' although the planted bug is in place"
    assert law_result(report, FULL_REFUND_LAW)["status"] == "fails"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} not found exactly once in {path}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def run_python(root: Path, code: str, **env: str) -> subprocess.CompletedProcess:
    """Plain Python with the project's src on PYTHONPATH (no lawkeep defences)."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        env=_env(PYTHONPATH=str(root / "src"), LAWKEEP_ROOT=str(root), **env),
    )


# Code an agent could drop into a side-door file to undo the planted bug while laws run
# (the planted bug adds the card-fee share to the refund total). It writes a marker file so the
# test can tell whether the file was executed at all.
SIDE_DOOR_PATCH = """\
import pathlib as _p, sys as _s
_here = _p.Path(__file__).resolve().parent
_root = _here if (_here / "lawkeep.toml").exists() else _here.parent
(_root / "{marker}").write_text(str(_here))
try:
    _s.path.insert(0, str(_root / "src"))
    import billing.refunds as _r
    _orig = _r.refund
    def _fixed(charge, amount):
        res = _orig(charge, amount)
        return _r.Refund(res.charge_id, res.total - res.fee_returned, res.fee_returned)
    _r.refund = _fixed
except Exception:
    pass
"""


def write_pyc(source: Path, pyc: Path, code_text: str) -> None:
    """Write ``code_text`` as bytecode that Python (or pytest) will accept for ``source``.

    The header carries the mtime and size of the CURRENT ``source`` file, so the stale bytecode
    passes the usual freshness check although it was compiled from different code.
    """
    st = source.stat()
    code = compile(code_text, str(source), "exec")
    header = importlib.util.MAGIC_NUMBER + b"\x00\x00\x00\x00"
    header += struct.pack("<LL", int(st.st_mtime) & 0xFFFFFFFF, st.st_size & 0xFFFFFFFF)
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(header + marshal.dumps(code))


def pyc_paths(source: Path) -> list[Path]:
    """The plain CPython cache file and pytest's assertion-rewrite cache file for ``source``."""
    plain = Path(importlib.util.cache_from_source(str(source)))
    rewritten = plain.with_name(
        f"{source.stem}.{sys.implementation.cache_tag}-pytest-{pytest.__version__}.pyc"
    )
    return [plain, rewritten]


# ---------------------------------------------------------------------------------------------
# Trick: edit the law itself


def test_trick_editing_a_law_is_flagged_as_unrecorded_law_change(tmp_path):
    root = make_project(tmp_path)
    replace_once(
        root / "laws/refunds.py",
        "    assert result.total <= charge.total\n",
        "    assert result.total <= charge.total * 2  # 'tolerance'\n",
    )
    assert "laws/refunds.py" in lock.diff_against_lock(root).changed

    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "laws/refunds.py", "error")
    assert code == 2, "an unrecorded law change waits for a person's decision (result code 2)"


def test_trick_editing_a_law_on_top_of_a_real_bug_still_fails(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    # Weaken only the law that names the bug most directly; the others still catch it.
    replace_once(
        root / "laws/refunds.py",
        "    assert result.total == charge.total\n",
        "    assert result.total >= charge.total\n",
    )
    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "laws/refunds.py", "error")
    assert code == 1
    assert (
        law_result(report, "refunds.partial_refund_is_exact_and_fee_share_is_fair")["status"]
        == "fails"
    )


def test_trick_deleting_a_law_file_is_flagged(tmp_path):
    root = make_project(tmp_path)
    (root / "laws/rounding.py").unlink()
    assert "laws/rounding.py" in lock.diff_against_lock(root).removed

    code, report = gate(root)
    assert_issue(
        report, ("unrecorded_law_removed", "unrecorded_law_change"), "laws/rounding.py", "error"
    )
    assert code == 2


# ---------------------------------------------------------------------------------------------
# Trick: edit a helper the laws depend on


def test_trick_editing_laws_strategies_is_flagged(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    # Only generate free charges: the planted bug never shows up on $0.00 charges.
    replace_once(
        root / "laws/strategies.py",
        "price=money(0, 1000)",
        "price=money(0, 0)",
    )
    assert "laws/strategies.py" in lock.diff_against_lock(root).changed

    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "laws/strategies.py", "error")
    assert code != 0


def test_trick_adding_a_skipping_conftest_inside_laws_is_flagged(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    (root / "laws/conftest.py").write_text(
        "import pytest\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.skip(reason='slow'))\n",
        encoding="utf-8",
    )
    assert "laws/conftest.py" in lock.diff_against_lock(root).added

    code, report = gate(root)
    assert_issue(
        report, ("unrecorded_law_added", "unrecorded_law_change"), "laws/conftest.py", "error"
    )
    assert code != 0
    assert law_result(report, FULL_REFUND_LAW)["status"] != "holds"


# ---------------------------------------------------------------------------------------------
# Trick: try fewer cases


def test_trick_lowering_examples_in_lawkeep_toml_is_flagged(tmp_path):
    root = make_project(tmp_path)
    replace_once(root / "lawkeep.toml", "examples    = 200\n", "examples    = 1\n")
    assert "lawkeep.toml" in lock.diff_against_lock(root).changed

    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "lawkeep.toml", "error")
    assert code == 2


def test_trick_settings_max_examples_1_on_a_law_is_flagged(tmp_path):
    root = make_project(tmp_path)
    replace_once(
        root / "laws/refunds.py",
        "from lawkeep import given, law, st\n",
        "from lawkeep import given, law, settings, st\n",
    )
    replace_once(
        root / "laws/refunds.py",
        '@law("A refund never exceeds the original payment")\n',
        '@law("A refund never exceeds the original payment")\n@settings(max_examples=1)\n',
    )
    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "laws/refunds.py", "error")
    assert code == 2


# ---------------------------------------------------------------------------------------------
# Trick: skip or xfail a law


SKIP_VARIANTS = {
    "skip_decorator": ("@pytest.mark.skip(reason='flaky')\n", ""),
    "xfail_decorator": ("@pytest.mark.xfail(reason='known issue')\n", ""),
    "skip_call_in_body": ("", "    pytest.skip('flaky')\n"),
}


@pytest.mark.parametrize("variant", sorted(SKIP_VARIANTS))
def test_trick_adding_skip_or_xfail_to_a_law_is_flagged(tmp_path, variant):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    decorator, body = SKIP_VARIANTS[variant]
    law_file = root / "laws/refunds.py"
    replace_once(law_file, '"""Laws about refunds', 'import pytest\n\n"""Laws about refunds')
    sentence = (
        '@law("Refunding exactly what was paid gives back exactly the payment and the whole '
        'card fee")\n'
    )
    replace_once(law_file, sentence, decorator + sentence)
    replace_once(
        law_file,
        "def full_refund_gives_back_exact_payment(charge):\n",
        "def full_refund_gives_back_exact_payment(charge):\n" + body,
    )

    code, report = gate(root)
    assert_issue(report, "unrecorded_law_change", "laws/refunds.py", "error")
    assert_issue(report, "skip_marker", "laws/refunds.py", None)
    assert law_result(report, FULL_REFUND_LAW)["status"] != "holds"
    assert code != 0


# ---------------------------------------------------------------------------------------------
# Trick: side-door files that run code before the laws


def test_trick_conftest_outside_laws_is_not_loaded_and_is_flagged(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    marker = root / "CONFTEST_WAS_LOADED"
    (root / "conftest.py").write_text(SIDE_DOOR_PATCH.format(marker=marker.name), encoding="utf-8")
    # The trick is real: a plain pytest run in this folder loads it.
    subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        env=_env(PYTHONDONTWRITEBYTECODE="1"),
    )
    assert marker.exists(), "sanity: plain pytest should load ./conftest.py"
    marker.unlink()

    code, report = gate(root)
    assert not marker.exists(), "lawkeep check loaded conftest.py from outside laws/"
    assert_issue(report, "side_door_file", "conftest.py", None)
    assert_refund_laws_not_passing(report)
    assert code == 1


@pytest.mark.parametrize("where", ["src/sitecustomize.py", "sitecustomize.py"])
def test_trick_sitecustomize_is_not_run_and_is_flagged(tmp_path, where):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    marker = root / "SITECUSTOMIZE_WAS_RUN"
    (root / where).write_text(SIDE_DOOR_PATCH.format(marker=marker.name), encoding="utf-8")
    if where.startswith("src/"):
        # The trick is real: Python runs sitecustomize.py from any folder on PYTHONPATH.
        run_python(root, "pass", PYTHONDONTWRITEBYTECODE="1")
        assert marker.exists(), "sanity: Python should run src/sitecustomize.py"
        marker.unlink()

    code, report = gate(root)
    assert not marker.exists(), f"lawkeep check ran {where}"
    assert_issue(report, "side_door_file", where, None)
    assert_refund_laws_not_passing(report)
    assert code == 1


def test_trick_usercustomize_and_pth_files_are_flagged(tmp_path):
    root = make_project(tmp_path)
    marker = root / "PTH_WAS_RUN"
    (root / "src/usercustomize.py").write_text(
        SIDE_DOOR_PATCH.format(marker=marker.name), encoding="utf-8"
    )
    (root / "src/zz_speedups.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pth')\n", encoding="utf-8"
    )
    code, report = gate(root)
    assert not marker.exists()
    assert_issue(report, "side_door_file", "src/usercustomize.py", None)
    assert_issue(report, "side_door_file", "src/zz_speedups.pth", None)


@pytest.mark.parametrize("ini", ["pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"])
def test_trick_root_pytest_config_cannot_deselect_laws(tmp_path, ini):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    section = (
        "[tool.pytest.ini_options]"
        if ini == "pyproject.toml"
        else ("[tool:pytest]" if ini == "setup.cfg" else "[pytest]")
    )
    value = '"-k not refund"' if ini == "pyproject.toml" else "-k 'not refund'"
    (root / ini).write_text(f"{section}\naddopts = {value}\n", encoding="utf-8")

    code, report = gate(root)
    assert_refund_laws_not_passing(report)
    assert code == 1


# ---------------------------------------------------------------------------------------------
# Trick: stale or patched bytecode with a matching timestamp


def test_trick_patched_code_pyc_with_matching_mtime_is_ignored(tmp_path):
    root = make_project(tmp_path)
    clean_source = (root / "src/billing/refunds.py").read_text(encoding="utf-8")
    apply_patch(root, "planted_bug.patch")
    buggy = root / "src/billing/refunds.py"
    # Bytecode of the CLEAN code, stamped with the BUGGY file's mtime and size.
    for pyc in pyc_paths(buggy)[:1]:
        write_pyc(buggy, pyc, clean_source + "\nTRICK = 'clean code from a stale pyc'\n")
    probe = run_python(root, "import billing.refunds as r; print(getattr(r, 'TRICK', 'source'))")
    assert "stale pyc" in probe.stdout, f"sanity: Python should load the pyc ({probe.stderr})"

    code, report = gate(root)
    assert_refund_laws_not_passing(report)
    assert code == 1


def test_trick_patched_law_pyc_with_matching_mtime_is_ignored_and_flagged(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    law_file = root / "laws/refunds.py"
    vacuous = (
        "from lawkeep import law\n"
        "TRICK = 'vacuous laws from a stale pyc'\n"
        '@law("A refund never exceeds the original payment")\n'
        "def refund_never_exceeds_payment():\n    pass\n"
        '@law("Refunding exactly what was paid gives back exactly the payment and the whole '
        'card fee")\n'
        "def full_refund_gives_back_exact_payment():\n    pass\n"
        '@law("A partial refund gives back exactly the amount asked for and a fair share of the '
        'card fee")\n'
        "def partial_refund_is_exact_and_fee_share_is_fair():\n    pass\n"
    )
    for pyc in pyc_paths(law_file):
        write_pyc(law_file, pyc, vacuous)
    assert lock.diff_against_lock(root).is_empty, "bytecode is not fingerprinted; source unchanged"
    probe = run_python(
        root,
        "import importlib.util as u\n"
        "s = u.spec_from_file_location('refund_laws', 'laws/refunds.py')\n"
        "m = u.module_from_spec(s); s.loader.exec_module(m)\n"
        "print(getattr(m, 'TRICK', 'source'))\n",
    )
    assert "stale pyc" in probe.stdout, f"sanity: Python should load the pyc ({probe.stderr})"

    code, report = gate(root)
    rel_pyc = pyc_paths(law_file)[0].relative_to(root).as_posix()
    assert_issue(report, "bytecode_file", rel_pyc, None)
    assert_refund_laws_not_passing(report)
    assert code == 1


# ---------------------------------------------------------------------------------------------
# Trick: tamper with the record itself


def test_trick_deleting_laws_lock_is_flagged(tmp_path):
    root = make_project(tmp_path)
    (root / "laws.lock").unlink()
    assert lock.read_lock(root) is None

    code, report = gate(root)
    assert_issue(report, "lock_missing", "laws.lock", "error")
    assert code != 0


def test_trick_editing_a_law_and_rewriting_laws_lock_by_hand_is_flagged(tmp_path):
    root = make_project(tmp_path)
    replace_once(
        root / "laws/refunds.py",
        "    assert result.total <= charge.total\n",
        "    pass\n",
    )
    lock.write_lock(root, lock.build_lock(root))  # what a cheating agent could script
    assert lock.diff_against_lock(root).is_empty
    unexplained = history.unexplained_lock_paths(lock.read_lock(root), history.read(root))
    assert "laws/refunds.py" in unexplained

    code, report = gate(root)
    hits = issues(report, "lock_rewritten")
    assert hits, f"laws.lock rewritten by hand was not flagged: {report.get('integrity')}"
    assert any(i.get("severity") == "error" for i in hits)
    assert code != 0


def test_trick_deleting_a_history_line_is_flagged(tmp_path):
    root = make_project(tmp_path)
    hist = root / "laws/history.jsonl"
    lines = hist.read_text(encoding="utf-8").splitlines(keepends=True)
    hist.write_text("".join(lines[:-1]), encoding="utf-8")
    assert lock.check_append_only(root, lock.read_lock(root))

    code, report = gate(root)
    assert_issue(
        report, ("append_only_violation", "history_unreadable"), "laws/history.jsonl", "error"
    )
    assert code != 0


def test_trick_removing_a_saved_failing_example_is_flagged(tmp_path):
    root = make_project(tmp_path)
    ledger = root / "laws/failing_examples.jsonl"
    apply_patch(root, "planted_bug.patch")
    code, first = run_lawkeep(root, "check")
    gate_built = not not_built(first)
    if gate_built:
        assert code == 1
        assert ledger.read_text(encoding="utf-8").strip(), (
            "check found failures but saved no failing example"
        )
    else:  # gate not built yet: add a minimal line by hand so the foundation part still runs
        ledger.write_text(
            json.dumps({"ts": "2026-11-21T10:00:00.000000Z", "law_id": FULL_REFUND_LAW}) + "\n",
            encoding="utf-8",
        )
    _git(root, "checkout", "--", "src")  # the agent fixes the code ...
    history.record_change(  # ... and the saved example is part of the record from now on
        root,
        action="add_example",
        actor="lawkeep",
        law_id=FULL_REFUND_LAW,
        direction="stricter",
        permission_used="auto",
        reason="saved a failing example found by lawkeep check",
    )
    # The trick: quietly drop the saved example.
    lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
    ledger.write_text("".join(lines[:-1]), encoding="utf-8")
    assert lock.check_append_only(root, lock.read_lock(root)), "foundation should spot this"

    code, report = gate(root)
    assert_issue(report, "append_only_violation", "laws/failing_examples.jsonl", "error")
    assert code == 2


# ---------------------------------------------------------------------------------------------
# Trick: change the code instead of the law, but only for the input the gate showed


def _total_from_example(example: dict | None) -> str | None:
    """The charged amount in a failing example, shown either as Hypothesis's call form
    ``charge(price=Decimal('0.25'), discount_code=None)`` or as a ``Charge(total=...)`` repr."""
    text = json.dumps(example or {}).replace("\\'", "'")
    m = re.search(r"(?:total|price)=Decimal\('([0-9.]+)'\)", text)
    return m.group(1) if m else None


def test_trick_special_casing_the_simplest_failing_example_still_fails(tmp_path):
    root = make_project(tmp_path)
    apply_patch(root, "planted_bug.patch")
    code, report = gate(root)
    assert code == 1
    first = law_result(report, FULL_REFUND_LAW)
    assert first["status"] == "fails"
    assert first["simplest_failing_example"], "every failure must carry its simplest input"
    total = _total_from_example(first["simplest_failing_example"]) or "0.25"

    # The cheat: return the right answer only for the exact input the gate printed.
    replace_once(
        root / "src/billing/refunds.py",
        "    amount = round_money(amount)\n",
        "    amount = round_money(amount)\n"
        f"    if charge.total == Decimal({total!r}) and amount == charge.total:\n"
        "        return Refund(charge_id=charge.id, total=charge.total, "
        "fee_returned=charge.card_fee)\n",
    )
    code, report = gate(root)
    second = law_result(report, FULL_REFUND_LAW)
    assert second["status"] == "fails", "special-casing one input must not make the law hold"
    assert second["simplest_failing_example"] != first["simplest_failing_example"]
    assert _total_from_example(second["simplest_failing_example"]) != total
    assert code == 1


# ---------------------------------------------------------------------------------------------
# Trick: route around the law with a new function


def test_trick_routing_around_a_law_with_refund_v2_shows_it_as_not_protected(tmp_path):
    root = make_project(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "agent-change")
    apply_patch(root, "unprotected_change.patch")
    _git(root, "commit", "-q", "-am", "add refund_v2 for the new checkout screen")

    code, report = gate(root)
    assert code == 0, "the laws only test refund(), so the gate is green"

    code, report = gate(root, "report", "--base", base)
    protection = report.get("protection") or {}
    functions = protection.get("functions") or []
    v2 = [f for f in functions if str(f.get("function", "")).endswith(":refund_v2")]
    assert v2, f"refund_v2 missing from the protection report: {functions}"
    assert v2[0]["status"] == "not_protected"
    assert v2[0].get("laws") in ([], None)
    unchanged = [f for f in functions if str(f.get("function", "")).endswith(":refund")]
    assert not unchanged, "refund() did not change and should not be listed as changed"
