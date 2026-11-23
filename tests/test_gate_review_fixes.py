"""Tests for the v0.1 review fixes in the gate: start-up isolation, results channel,
site-packages false positives, the source scan and unconfirmed approvals."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from lawkeep import config, history, integrity, lawrun
from lawkeep.models import utc_now_iso

FIX = Path(__file__).parent / "fixtures" / "lifecycle"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


# --------------------------------------------------------------------------- _boot / -S


def test_boot_adds_pth_paths_but_never_runs_pth_code(tmp_path):
    site = tmp_path / "site"
    extra = tmp_path / "extra"
    site.mkdir()
    extra.mkdir()
    (extra / "fromextra.py").write_text("VALUE = 42\n")
    marker = tmp_path / "pth-ran"
    (site / "_virtualenv.pth").write_text("import _virtualenv\n")
    (site / "evil.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')\n{extra}\n"
    )
    (site / "sitecustomize.py").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('sitecustomize')\n"
    )
    probe = tmp_path / "probe_mod"
    probe.mkdir()
    (probe / "showpath.py").write_text(
        "import sys, fromextra\nprint(fromextra.VALUE, 'site' in sys.modules)\n"
    )
    argv = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        str(lawrun.BOOT_SCRIPT),
        json.dumps([str(site), str(probe)]),
        "showpath",
    ]
    out = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["42", "False"]
    assert not marker.exists()


def test_clean_env_drops_caller_lawkeep_variables(monkeypatch):
    monkeypatch.setenv("LAWKEEP_EXTRA_SYS_PATH", "/somewhere/clean")
    monkeypatch.setenv("PYTHONPATH", "/x")
    env = lawrun.clean_env({"LAWKEEP_ROOT": "/r"})
    assert "LAWKEEP_EXTRA_SYS_PATH" not in env and "PYTHONPATH" not in env
    assert env["LAWKEEP_ROOT"] == "/r" and env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_results_pipe_cannot_be_rewritten_after_it_is_closed(tmp_path):
    code = (
        "import os, sys, json, atexit\n"
        "req = json.load(sys.stdin)\n"
        "assert 'LAWKEEP_RESULTS_FD' not in os.environ\n"
        "fd = req['results_fd']\n"
        "os.write(fd, json.dumps({'echo': req['hello']}).encode()); os.close(fd)\n"
        "atexit.register(lambda: print('late'))\n"
    )
    cap = lawrun.run_capture(
        [sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ), request={"hello": 1}
    )
    assert cap.returncode == 0, cap.stderr
    assert json.loads(cap.data) == {"echo": 1}


# ----------------------------------------------------------- site-packages false positives


def test_virtualenv_pth_files_are_not_side_doors(tmp_path, monkeypatch):
    sp = tmp_path / "site-packages"
    sp.mkdir()
    (sp / "_virtualenv.pth").write_text("import _virtualenv\n")
    (sp / "a1_coverage.pth").write_text(
        'import sys; exec(\'import os\\nif os.getenv("COVERAGE_PROCESS_START"):\\n '
        'import coverage\\n coverage.process_startup(slug="pth")\')\n'
    )
    (sp / "_editable_impl_shop.pth").write_text(str(tmp_path) + "\n")
    (sp / "odd.pth").write_text("import os; os.system('true')\n")
    monkeypatch.setattr(integrity, "_site_packages_dirs", lambda: [sp])
    monkeypatch.setattr(integrity, "_lock_time", lambda root: 0.0)
    found = integrity._site_packages_issues(tmp_path)
    assert [Path(i.path or "").name for i in found] == ["odd.pth"]
    assert found[0].severity == "warning"  # the gate never runs it (python -S)


def test_fresh_venv_does_not_make_the_examples_wait(tmp_path, monkeypatch):
    """Done-when 1 in CI: a venv created after laws.lock must not turn the gate into 'wait'."""
    root = tmp_path / "billing"
    shutil.copytree(
        Path(__file__).resolve().parents[1] / "examples" / "billing",
        root,
        ignore=shutil.ignore_patterns("__pycache__", ".hypothesis", ".lawkeep"),
    )
    sp = tmp_path / "venv-site"
    sp.mkdir()
    (sp / "_virtualenv.pth").write_text("import _virtualenv\n")
    os.utime(sp / "_virtualenv.pth")  # newer than laws.lock
    monkeypatch.setattr(integrity, "_site_packages_dirs", lambda: [sp])
    assert integrity._site_packages_issues(root) == []
    cfg = config.load_config(root)
    assert not [i for i in integrity.check_integrity(root, cfg) if i.severity == "error"]


def test_state_machine_failure_shows_its_steps():
    from lawkeep.pytest_plugin import examples_from_exception

    exc = AssertionError("boom")
    # What Hypothesis attaches (add_note and the typed attribute are Python 3.11+ only).
    exc.__notes__ = [  # pyright: ignore[reportAttributeAccessIssue]
        "Failing test case: <state machine>",
        "state = StackMachine()",
        "state.push(x=0)",
        "state.size()",
        "state.teardown()",
    ]
    ex = examples_from_exception(exc)[0]
    assert ex["call"].splitlines()[:2] == ["state = StackMachine()", "state.push(x=0)"]


# ----------------------------------------------------------------- isolation of strength/probe


def _teashop(tmp_path: Path) -> Path:
    root = tmp_path / "teashop"
    shutil.copytree(FIX / "teashop", root)
    (root / "lawkeep.toml").write_text('[agent]\nname = "other"\n', encoding="utf-8")
    for name in ("history.jsonl", "failing_examples.jsonl"):
        (root / "laws" / name).write_text("", encoding="utf-8")
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def test_probe_and_strength_do_not_run_project_sitecustomize(tmp_path):
    from lawkeep import examples_table, strength

    root = _teashop(tmp_path)
    marker = tmp_path / "sitecustomize-ran"
    (root / "sitecustomize.py").write_text(
        f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv))\n"
    )
    cfg = config.load_config(root)
    law = root / "laws" / "pricing.py"
    data = examples_table.run_probe(
        root,
        cfg,
        [{"name": "v", "file": str(law), "qualname": "discount_never_raises_price"}],
        examples=5,
    )
    assert "error" not in data, data
    strength.run_quick_check(root, cfg, law_ids=["pricing.discount_never_raises_price"])
    assert not marker.exists(), marker.read_text()


# ------------------------------------------------------------------------------ source scan


def test_scan_source_flags_runner_imports_and_shadowing(tmp_path):
    root = _teashop(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    pkg = root / "src" / "teashop"
    (pkg / "sneaky.py").write_text("import os\nX = os.environ.get('LAWKEEP_ROOT')\n")
    (root / "src" / "fractions.py").write_text("# shadows the standard library\n")
    issues = integrity.scan_source(root, config.load_config(root))
    by_path = {i.path: i for i in issues}
    assert by_path["src/teashop/sneaky.py"].severity == "error"
    assert by_path["src/fractions.py"].severity == "error"
    assert "standard library" in by_path["src/fractions.py"].message
    # the untouched pricing module is not flagged
    assert "src/teashop/pricing.py" not in by_path


def test_scan_source_without_changes_is_quiet_for_normal_code(tmp_path):
    root = _teashop(tmp_path)
    assert integrity.scan_source(root, config.load_config(root)) == []


# ------------------------------------------------------------------------ unconfirmed approval


def test_approvals_without_a_terminal_are_shown(tmp_path):
    root = _teashop(tmp_path)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    history.record_change(
        root,
        action="relock",
        actor="person:Anyone",
        approved_by="person:Anyone",
        details={"channel": "python"},
    )
    cfg = config.load_config(root)
    found = [i for i in integrity.check_integrity(root, cfg) if i.kind == "approval_unconfirmed"]
    assert found and "person:Anyone" in found[0].message and found[0].severity == "warning"
    history.record_change(
        root,
        action="relock",
        actor="person:Ada",
        approved_by="person:Ada",
        details={"channel": "terminal", "terminal": True, "at": utc_now_iso()},
    )
    found = [i for i in integrity.check_integrity(root, cfg) if i.kind == "approval_unconfirmed"]
    assert "person:Ada" not in found[0].message


def test_lambda_built_values_are_shown_as_what_they_build():
    from lawkeep.output import plain_example

    raw = "[(lambda start, steps: Slot(start, start + steps * STEP))(OPENING, 2)]"
    assert plain_example({"requests": raw}) == "requests=[Slot(OPENING, OPENING + 2 * STEP)]"
    assert plain_example({"x": "f(lambda y: y)"}) == "x=f(lambda y: y)"
