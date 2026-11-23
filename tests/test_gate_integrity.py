"""Gate (B): the integrity check (no code runs): unrecorded law changes and side doors."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from lawkeep import config, history, integrity, ledger, lock

FIXTURES = Path(__file__).parent / "fixtures" / "gate"


def make_project(tmp_path: Path, *, init: bool = True, git: bool = False) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(FIXTURES / "refunds_project", root)
    (root / "laws" / "history.jsonl").touch()
    (root / "laws" / "failing_examples.jsonl").touch()
    if init:
        history.record_change(root, action="init", actor="person:tester", direction="stricter")
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "start")
    return root


def _git(root: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Tester",
        GIT_AUTHOR_EMAIL="tester@example.com",
        GIT_COMMITTER_NAME="Tester",
        GIT_COMMITTER_EMAIL="tester@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    out = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def check(root: Path, **kw):
    return integrity.check_integrity(root, config.load_config(root), **kw)


def kinds(issues, severity=None):
    return sorted(i.kind for i in issues if severity is None or i.severity == severity)


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))
    return p


def test_clean_project_has_no_errors(tmp_path):
    root = make_project(tmp_path)
    issues = check(root)
    assert kinds(issues, "error") == []
    assert kinds(issues) == ["git_unavailable"]  # info: no git here, said plainly


def test_clean_git_project_has_no_issues(tmp_path):
    root = make_project(tmp_path, git=True)
    assert check(root) == []


def test_hand_edited_law_is_an_unrecorded_change(tmp_path):
    root = make_project(tmp_path)
    p = root / "laws" / "refunds.py"
    p.write_text(p.read_text().replace("<= charge", "<= charge + 1000"))
    issues = [i for i in check(root) if i.kind == "unrecorded_law_change"]
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error" and issue.path == "laws/refunds.py"
    assert "unrecorded law change" in issue.message and "edited directly" in issue.message
    assert "refunds.refund_is_capped" in issue.message
    assert issue.details["laws"] == ["refunds.refund_is_capped"]


def test_sentence_change_is_named(tmp_path):
    root = make_project(tmp_path)
    p = root / "laws" / "refunds.py"
    p.write_text(p.read_text().replace("A refund is never negative", "A refund is usually fine"))
    issue = next(i for i in check(root) if i.kind == "unrecorded_law_change")
    assert "“A refund is never negative” → “A refund is usually fine”" in issue.message


def test_added_and_removed_law_files(tmp_path):
    root = make_project(tmp_path)
    write(root, "laws/new.py", 'from lawkeep import law\n\n@law("x")\ndef x():\n    pass\n')
    (root / "laws" / "strategies.py").unlink()
    issues = check(root)
    added = next(i for i in issues if i.kind == "unrecorded_law_added")
    removed = next(i for i in issues if i.kind == "unrecorded_law_removed")
    assert added.path == "laws/new.py" and added.details["laws"] == ["new.x"]
    assert removed.path == "laws/strategies.py"


def test_settings_change_is_flagged(tmp_path):
    root = make_project(tmp_path)
    (root / "lawkeep.toml").write_text("[check.pr]\nexamples = 1\n")
    issue = next(i for i in check(root) if i.kind == "unrecorded_law_change")
    assert issue.path == "lawkeep.toml" and "Settings can weaken every law" in issue.message


def test_recorded_change_is_not_flagged(tmp_path):
    root = make_project(tmp_path)
    write(root, "laws/new.py", 'from lawkeep import law\n\n@law("x")\ndef x():\n    pass\n')
    history.record_change(
        root,
        action="add_law",
        actor="agent:test",
        law_id="new.x",
        direction="stricter",
        permission_used="auto",
    )
    assert kinds(check(root), "error") == []


def test_lock_regenerated_by_hand_is_flagged(tmp_path):
    root = make_project(tmp_path)
    p = root / "laws" / "refunds.py"
    p.write_text(p.read_text().replace("<= charge", "<= charge * 2"))
    lock.write_lock(root, lock.build_lock(root))  # what a tampering agent would do
    issues = check(root)
    assert "unrecorded_law_change" not in kinds(issues)
    rewritten = next(i for i in issues if i.kind == "lock_rewritten")
    assert "laws/refunds.py" in rewritten.details["paths"] and rewritten.severity == "error"


def test_removed_saved_failure_is_flagged(tmp_path):
    root = make_project(tmp_path)
    ledger.add(root, "refunds.refund_is_capped", {"amount": "1001"}, choices=b"\x01")
    history.record_change(
        root,
        action="add_example",
        actor="lawkeep",
        law_id="refunds.refund_is_capped",
        direction="stricter",
    )
    (root / "laws" / "failing_examples.jsonl").write_text("")
    issue = next(i for i in check(root) if i.kind == "append_only_violation")
    assert issue.path == "laws/failing_examples.jsonl" and issue.severity == "error"


def test_new_saved_failures_after_lock_are_fine(tmp_path):
    root = make_project(tmp_path)
    ledger.add(root, "refunds.refund_is_capped", {"amount": "1001"}, choices=b"\x01")
    assert kinds(check(root), "error") == []


def test_git_catches_removal_of_unlocked_saved_failure(tmp_path):
    root = make_project(tmp_path, git=True)
    ledger.add(root, "refunds.refund_is_capped", {"amount": "1001"}, choices=b"\x01")
    _git(root, "commit", "-qam", "found a failure")
    (root / "laws" / "failing_examples.jsonl").write_text("")
    issues = check(root)
    issue = next(i for i in issues if i.kind == "append_only_violation")
    assert "compared with git HEAD" in issue.message


def test_unreadable_history_is_an_issue_not_a_crash(tmp_path):
    root = make_project(tmp_path)
    with open(root / "laws" / "history.jsonl", "a") as f:
        f.write("this is not json\n")
    assert "history_unreadable" in kinds(check(root), "error")


def test_missing_and_broken_lock(tmp_path):
    root = make_project(tmp_path, init=False)
    assert "lock_missing" in kinds(check(root), "error")
    (root / "laws.lock").write_text("<<<<<<< HEAD\n")
    assert "lock_unreadable" in kinds(check(root), "error")


def test_skip_marker_added_silently_is_an_error(tmp_path):
    root = make_project(tmp_path)
    p = root / "laws" / "refunds.py"
    text = p.read_text().replace(
        '@law("A refund is never negative")',
        '@pytest.mark.skip(reason="later")\n@law("A refund is never negative")',
    )
    p.write_text("import pytest\n" + text)
    issues = [i for i in check(root) if i.kind == "skip_marker"]
    assert len(issues) == 1 and issues[0].severity == "error"
    assert "pytest.mark.skip" in issues[0].message


def test_recorded_skip_marker_is_only_a_warning(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/skipped.py",
        """
        import pytest
        from lawkeep import law

        @law("A law a person agreed to skip")
        def later():
            pytest.skip("approved for now")
        """,
    )
    history.record_change(
        root,
        action="skip_law",
        actor="agent:test",
        law_id="skipped.later",
        direction="looser",
        permission_used="ask",
        approved_by="person:tester",
    )
    issues = [i for i in check(root) if i.kind == "skip_marker"]
    assert [i.severity for i in issues] == ["warning"]


def test_fewer_examples_settings_are_flagged(tmp_path):
    root = make_project(tmp_path)
    write(
        root,
        "laws/few.py",
        """
        from hypothesis import settings, Phase
        from lawkeep import law, given, st

        settings.register_profile("tiny", max_examples=1)

        @law("x")
        @settings(max_examples=1, phases=[Phase.explicit])
        @given(x=st.integers())
        def x(x):
            pass
        """,
    )
    issues = [i for i in check(root) if i.kind == "weakened_settings"]
    text = " ".join(i.message for i in issues)
    assert "max_examples=1" in text and "phases" in text and "register_profile" in text
    assert all(i.severity == "error" for i in issues)  # file not recorded


def test_conftest_outside_laws_without_git_is_info(tmp_path):
    root = make_project(tmp_path)
    write(root, "tests/conftest.py", "import os\n")
    issue = next(i for i in check(root) if i.kind == "side_door_file")
    assert issue.path == "tests/conftest.py" and issue.severity == "info"


def test_conftest_outside_laws_with_git_only_when_changed(tmp_path):
    root = make_project(tmp_path, git=False)
    write(root, "tests/conftest.py", "import os\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    assert [i for i in check(root) if i.kind == "side_door_file"] == []
    write(root, "conftest.py", "import shop.refunds\n")
    issue = next(i for i in check(root) if i.kind == "side_door_file")
    assert issue.path == "conftest.py" and issue.severity == "warning"
    assert "new or changed" in issue.message


def test_sitecustomize_and_pth_are_flagged(tmp_path):
    root = make_project(tmp_path)
    write(root, "src/sitecustomize.py", "import os\n")
    write(root, "src/evil.pth", "import os\n")
    paths = sorted(i.path or "" for i in check(root) if i.kind == "side_door_file")
    assert paths == ["src/evil.pth", "src/sitecustomize.py"]


def test_hooks_in_laws_conftest_are_flagged(tmp_path):
    root = make_project(tmp_path)
    write(root, "laws/conftest.py", "def pytest_runtest_makereport(item, call):\n    pass\n")
    issue = next(i for i in check(root) if i.kind == "laws_conftest_hook")
    assert issue.details["hooks"] == ["pytest_runtest_makereport"]
    assert issue.severity == "error"  # added without lawkeep


def _patched_pyc(src: Path, other_source: str) -> Path:
    from importlib._bootstrap_external import (
        _code_to_timestamp_pyc,  # pyright: ignore[reportAttributeAccessIssue]  # private CPython helper, not in typeshed
    )

    st_ = src.stat()
    code = compile(other_source, str(src), "exec", dont_inherit=True)
    pyc = Path(importlib.util.cache_from_source(str(src)))
    pyc.parent.mkdir(exist_ok=True)
    pyc.write_bytes(_code_to_timestamp_pyc(code, int(st_.st_mtime), st_.st_size))
    return pyc


def test_patched_bytecode_is_flagged(tmp_path):
    root = make_project(tmp_path)
    src = root / "src" / "shop" / "refunds.py"
    pyc = _patched_pyc(src, (FIXTURES / "buggy_refunds.py").read_text())
    issue = next(i for i in check(root) if i.kind == "bytecode_file")
    assert issue.severity == "error" and "looks patched" in issue.message
    assert issue.path is not None and issue.path.endswith(pyc.name)


def test_honest_bytecode_is_not_flagged(tmp_path):
    root = make_project(tmp_path)
    src = root / "src" / "shop" / "refunds.py"
    _patched_pyc(src, src.read_text())
    assert "bytecode_file" not in kinds(check(root))
    assert integrity.check_bytecode(Path(importlib.util.cache_from_source(str(src)))) is None


def test_stale_bytecode_is_harmless(tmp_path):
    root = make_project(tmp_path)
    src = root / "src" / "shop" / "refunds.py"
    _patched_pyc(src, "x = 1\n")
    time.sleep(0.01)
    src.write_text(src.read_text() + "\n# edited\n")  # size changes -> header no longer valid
    assert "bytecode_file" not in kinds(check(root))


def test_sourceless_pyc_is_flagged(tmp_path):
    root = make_project(tmp_path)
    (root / "src" / "shop" / "helper.pyc").write_bytes(b"\x00" * 20)
    issue = next(i for i in check(root) if i.kind == "bytecode_file")
    assert issue.severity == "error" and "no matching .py source" in issue.message


def test_site_packages_side_door_after_lock(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    fake_sp = tmp_path / "site-packages"
    fake_sp.mkdir()
    old = fake_sp / "old.pth"
    old.write_text("import os\n")
    os.utime(old, (1, 1))  # long before the lock
    (fake_sp / "sitecustomize.py").write_text("import os\n")
    (fake_sp / "paths_only.pth").write_text("/some/path\n")
    monkeypatch.setattr(integrity, "_site_packages_dirs", lambda: [fake_sp])
    issues = [i for i in check(root) if i.kind == "site_packages_side_door"]
    assert [Path(i.path or "").name for i in issues] == ["sitecustomize.py"]


def test_integrity_runs_no_project_code(tmp_path):
    root = make_project(tmp_path)
    marker = tmp_path / "ran"
    write(root, "laws/boom.py", f"open({str(marker)!r}, 'w').write('x')\n")
    write(root, "laws/conftest.py", f"open({str(marker)!r}, 'w').write('x')\n")
    check(root)
    assert not marker.exists()


@pytest.mark.parametrize("ref", ["HEAD", "does-not-exist"])
def test_base_ref(tmp_path, ref):
    root = make_project(tmp_path, git=True)
    issues = check(root, base_ref=ref)
    if ref == "HEAD":
        assert issues == []
    else:
        assert any("was not found" in i.message for i in issues)
