"""Tests for lawkeep.lock (laws.lock) and lawkeep.history (laws/history.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lawkeep import history, lock
from lawkeep.errors import HistoryError, LockError
from lawkeep.models import HistoryEntry


@pytest.fixture(autouse=True)
def _no_root_env(monkeypatch):
    monkeypatch.delenv("LAWKEEP_ROOT", raising=False)


LAW = "from lawkeep import law\n\n@law('A refund never exceeds the charge')\ndef capped():\n    assert True\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "lawkeep.toml").write_text('[permissions]\nadd_law = "auto"\n')
    laws = tmp_path / "laws"
    laws.mkdir()
    (laws / "refunds.py").write_text(LAW)
    (laws / "strategies.py").write_text("from lawkeep import st\n")
    (laws / "data").mkdir()
    (laws / "data" / "cases.json").write_text("[]")
    # things that must NOT be fingerprinted
    (laws / "drafts").mkdir()
    (laws / "drafts" / "new.py").write_text(LAW)
    (laws / "__pycache__").mkdir()
    (laws / "__pycache__" / "refunds.cpython-312.pyc").write_bytes(b"\0")
    (laws / ".hypothesis").mkdir()
    (laws / ".hypothesis" / "x").write_text("x")
    (laws / "stray.pyc").write_bytes(b"\0")
    (laws / "history.jsonl").write_text("")
    (laws / "failing_examples.jsonl").write_text("")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    return tmp_path


def test_fingerprint_covers_the_right_files(project):
    fp = lock.fingerprint(project)
    assert sorted(fp) == sorted(
        ["laws/data/cases.json", "laws/refunds.py", "laws/strategies.py", "lawkeep.toml"]
    )
    assert all(v.startswith("sha256:") and len(v) == 71 for v in fp.values())
    assert lock.is_fingerprinted(project, "laws/new_file.py")
    assert not lock.is_fingerprinted(project, "laws/drafts/x.py")
    assert not lock.is_fingerprinted(project, "laws/history.jsonl")
    assert not lock.is_fingerprinted(project, "src/app.py")


def test_build_write_read_round_trip(project):
    built = lock.build_lock(project)
    assert "refunds.capped" in built.laws
    assert built.laws["refunds.capped"].sentence == "A refund never exceeds the charge"
    assert built.laws["refunds.capped"].file == "laws/refunds.py"
    assert set(built.append_only) == {"laws/history.jsonl", "laws/failing_examples.jsonl"}
    path = lock.write_lock(project, built)
    text = path.read_text()
    assert text.startswith("# laws.lock")
    again = lock.read_lock(project)
    assert again is not None
    assert again.to_dict() == built.to_dict()
    assert lock.diff_against_lock(project).is_empty


def test_compare_detects_changes(project):
    lock.write_lock(project, lock.build_lock(project))
    (project / "laws" / "refunds.py").write_text(LAW.replace("True", "False"))
    (project / "laws" / "extra.py").write_text("x = 1\n")
    (project / "laws" / "data" / "cases.json").unlink()
    (project / "laws" / "drafts" / "new.py").write_text("changed")  # ignored
    diff = lock.diff_against_lock(project)
    assert diff.changed == ["laws/refunds.py"]
    assert diff.added == ["laws/extra.py"]
    assert diff.removed == ["laws/data/cases.json"]
    assert diff.paths() == ["laws/data/cases.json", "laws/extra.py", "laws/refunds.py"]
    assert not diff.is_empty
    assert lock.compare(None, {"a": "1"}).added == ["a"]


def test_settings_change_is_detected(project):
    lock.write_lock(project, lock.build_lock(project))
    (project / "lawkeep.toml").write_text("[check.pr]\nexamples = 1\n")
    assert lock.diff_against_lock(project).changed == ["lawkeep.toml"]


def test_no_lock(project):
    assert lock.read_lock(project) is None
    assert set(lock.diff_against_lock(project).added) == set(lock.fingerprint(project))


def test_bad_lock(project):
    (project / "laws.lock").write_text("<<<<<<< HEAD\n")
    with pytest.raises(LockError, match="not valid TOML"):
        lock.read_lock(project)
    (project / "laws.lock").write_text("version = 99\n")
    with pytest.raises(LockError, match="version 99"):
        lock.read_lock(project)
    (project / "laws.lock").write_text("version = 1\n[append_only.x]\nlines = 1\n")
    with pytest.raises(LockError, match="unexpected shape"):
        lock.read_lock(project)


def test_append_only_checks(project):
    hist = project / "laws" / "history.jsonl"
    hist.write_text('{"a": 1}\n{"b": 2}\n')
    current = lock.build_lock(project)
    assert lock.check_append_only(project, current) == []
    with hist.open("a") as f:
        f.write('{"c": 3}\n')
    assert lock.check_append_only(project, current) == []
    hist.write_text('{"a": 1}\n')
    problems = lock.check_append_only(project, current)
    assert len(problems) == 1 and "lost lines" in problems[0].message
    hist.write_text('{"a": 1}\n{"b": 999}\n{"c": 3}\n')
    problems = lock.check_append_only(project, current)
    assert len(problems) == 1 and "was edited" in problems[0].message
    assert lock.check_append_only(project, None) == []


def _entry(**kw: Any) -> HistoryEntry:
    base: dict[str, Any] = dict(
        action="add_law", actor="agent:test", law_id="refunds.capped", direction="stricter"
    )
    base.update(kw)
    return HistoryEntry(**base)


def test_history_append_and_read(project):
    e1 = history.append(project, _entry(reason="first"))
    assert e1.id.startswith("h-") and e1.timestamp.endswith("Z")
    e2 = history.append(project, _entry(reason="second ✓", status="waiting", direction="looser"))
    entries = history.read(project)
    assert [e.reason for e in entries] == ["first", "second ✓"]
    assert entries[1].to_dict() == e2.to_dict()
    found = history.find(entries, e2.id)
    assert found is not None and found.reason == "second ✓"
    assert history.find(entries, "nope") is None
    assert history.since(project, e2.timestamp) == [entries[1]]
    assert len(history.since(project, None)) == 2
    assert history.pending(entries) == [entries[1]]
    history.append(project, _entry(status="rejected", refers_to=e2.id, approved_by="person:Ada"))
    assert history.pending(history.read(project)) == []


def test_history_append_repairs_missing_newline(project):
    hist = project / "laws" / "history.jsonl"
    hist.write_text(json.dumps(_entry().to_dict()))  # no trailing newline
    history.append(project, _entry(reason="next"))
    assert len(history.read(project)) == 2


def test_history_malformed_line(project):
    (project / "laws" / "history.jsonl").write_text(
        '{"action": "add_law", "actor": "x"}\nnot json\n'
    )
    with pytest.raises(HistoryError, match="line 2"):
        history.read(project)
    (project / "laws" / "history.jsonl").write_text(
        '{"action": "add_law", "actor": "x", "direction": "sideways"}\n'
    )
    with pytest.raises(HistoryError, match="line 1"):
        history.read(project)


def test_history_missing_file(tmp_path):
    assert history.read(tmp_path) == []


def test_record_change_full_cycle(project):
    # init: everything is new
    rec = history.record_change(project, action="init", actor="person:Ada", direction="stricter")
    assert set(rec.entry.files) == set(lock.fingerprint(project))
    assert lock.diff_against_lock(project).is_empty
    assert lock.check_append_only(project, lock.read_lock(project)) == []
    entries = history.read(project)
    assert history.unexplained_lock_paths(lock.read_lock(project), entries) == []

    # a recorded law change
    (project / "laws" / "fees.py").write_text(LAW.replace("capped", "fee"))
    rec = history.record_change(
        project,
        action="add_law",
        actor="agent:claude-code",
        law_id="fees.fee",
        direction="stricter",
        permission_used="auto",
        sentence_after="A refund never exceeds the charge",
    )
    assert list(rec.entry.files) == ["laws/fees.py"]
    assert rec.lock is not None and "fees.fee" in rec.lock.laws
    assert lock.diff_against_lock(project).is_empty
    entries = history.read(project)
    assert history.unexplained_lock_paths(lock.read_lock(project), entries) == []
    explained = history.explains(entries, "laws/fees.py", rec.entry.files["laws/fees.py"])
    assert explained is not None and explained.id == rec.entry.id

    # a waiting proposal does not touch the lock
    before = (project / "laws.lock").read_text()
    rec = history.record_change(
        project, action="change_law", actor="agent:x", direction="looser", status="waiting"
    )
    assert rec.lock is None and (project / "laws.lock").read_text() == before
    assert lock.check_append_only(project, lock.read_lock(project)) == []

    # deletion recorded as None
    (project / "laws" / "fees.py").unlink()
    rec = history.record_change(
        project,
        action="remove_law",
        actor="person:Ada",
        law_id="fees.fee",
        direction="looser",
        permission_used="ask",
        approved_by="person:Ada",
    )
    assert rec.entry.files == {"laws/fees.py": None}
    assert history.unexplained_lock_paths(lock.read_lock(project), history.read(project)) == []


def test_unrecorded_edit_and_hand_rewritten_lock_are_detected(project):
    history.record_change(project, action="init", actor="person:Ada", direction="stricter")
    # 1. agent edits a law directly: lock differs
    (project / "laws" / "refunds.py").write_text(LAW.replace("True", "1 == 1 or True"))
    assert lock.diff_against_lock(project).changed == ["laws/refunds.py"]
    # 2. agent also regenerates laws.lock by hand: lock matches, but history does not explain it
    lock.write_lock(project, lock.build_lock(project))
    assert lock.diff_against_lock(project).is_empty
    assert history.unexplained_lock_paths(lock.read_lock(project), history.read(project)) == [
        "laws/refunds.py"
    ]


def test_record_change_with_no_file_changes(project):
    history.record_change(project, action="init", actor="person:Ada", direction="stricter")
    rec = history.record_change(project, action="relock", actor="lawkeep")
    assert rec.entry.files == {} and rec.notes


def test_session_marker(project):
    assert history.session_start(project) is None
    ts = history.mark_session_start(project, agent="codex")
    assert history.session_start(project) == ts
    (project / ".lawkeep" / "session.json").write_text("garbage")
    assert history.session_start(project) is None


def test_default_person(tmp_path, monkeypatch):
    who = history.default_person(tmp_path)
    assert who.startswith("person:") and len(who) > len("person:")
