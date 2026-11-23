"""Tests for lawkeep.paths, lawkeep.exitcodes and lawkeep.errors."""

from __future__ import annotations

import pytest

from lawkeep import exitcodes, paths
from lawkeep.errors import LawkeepError, ProjectNotFound, SetupError


@pytest.fixture(autouse=True)
def _no_root_env(monkeypatch):
    monkeypatch.delenv("LAWKEEP_ROOT", raising=False)


def test_exit_codes():
    assert (
        exitcodes.ALL_HOLD,
        exitcodes.SOME_FAIL,
        exitcodes.WAITING,
        exitcodes.SETUP_PROBLEM,
    ) == (0, 1, 2, 3)
    assert exitcodes.combine() == 0
    assert exitcodes.combine(0, 2) == 2
    assert exitcodes.combine(2, 1, 0) == 1
    assert exitcodes.combine(1, 3) == 3
    assert exitcodes.verdict(0) == "ALL LAWS HOLD"
    assert exitcodes.verdict(1) == "SOME LAWS FAIL"
    with pytest.raises(ValueError):
        exitcodes.combine(9)
    with pytest.raises(ValueError):
        exitcodes.verdict(9)


def test_find_root_prefers_lawkeep_toml(tmp_path):
    (tmp_path / "lawkeep.toml").write_text("")
    inner = tmp_path / "pkg" / "laws"
    inner.mkdir(parents=True)
    deep = inner / "x"
    deep.mkdir()
    assert paths.find_root(deep) == tmp_path.resolve()


def test_find_root_falls_back_to_laws_dir(tmp_path):
    (tmp_path / "laws").mkdir()
    (tmp_path / "src" / "app").mkdir(parents=True)
    assert paths.find_root(tmp_path / "src" / "app") == tmp_path.resolve()
    f = tmp_path / "src" / "app" / "m.py"
    f.write_text("")
    assert paths.find_root(f) == tmp_path.resolve()


def test_find_root_none_and_require(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.find_root() is None
    with pytest.raises(ProjectNotFound) as info:
        paths.require_root()
    assert "lawkeep init" in str(info.value)
    assert isinstance(info.value, SetupError) and isinstance(info.value, LawkeepError)
    assert info.value.exit_code == 3


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LAWKEEP_ROOT", str(tmp_path))
    assert paths.find_root() == tmp_path.resolve()
    other = tmp_path / "other"
    (other / "laws").mkdir(parents=True)
    assert paths.find_root(other) == other.resolve()  # an explicit start (--root) wins


def test_project_paths_layout(tmp_path):
    p = paths.project_paths(tmp_path)
    r = tmp_path.resolve()
    assert p.config == r / "lawkeep.toml"
    assert p.lock == r / "laws.lock"
    assert p.laws == r / "laws"
    assert p.drafts == r / "laws" / "drafts"
    assert p.history == r / "laws" / "history.jsonl"
    assert p.ledger == r / "laws" / "failing_examples.jsonl"
    assert p.strategies == r / "laws" / "strategies.py"
    assert p.state == r / ".lawkeep"
    assert paths.session_file(r) == r / ".lawkeep" / "session.json"
    assert set(p.to_dict()) >= {"root", "lock", "history"}


def test_rel_and_is_draft(tmp_path):
    assert paths.rel(tmp_path, tmp_path / "laws" / "a.py") == "laws/a.py"
    assert paths.rel(tmp_path, "laws/a.py") == "laws/a.py"
    assert paths.rel(tmp_path / "x", tmp_path / "y.py").endswith("/y.py")
    assert paths.is_draft(tmp_path, "laws/drafts/a.py")
    assert paths.is_draft(tmp_path, tmp_path / "laws" / "drafts")
    assert not paths.is_draft(tmp_path, "laws/draftsy.py")
    assert not paths.is_draft(tmp_path, "laws/a.py")
