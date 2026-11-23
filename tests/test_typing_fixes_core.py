"""Fixes found while making the core modules pass pyright strict (2026-11-23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lawkeep import cli, history
from lawkeep.errors import HistoryError
from lawkeep.models import HistoryEntry, as_dict, as_list


def _write_history(root: Path, entry: dict[str, object]) -> None:
    path = root / "laws" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "bad",
    [{"details": ["not", "a", "dict"]}, {"files": "laws/x.py"}, {"details": "text"}],
)
def test_history_entry_with_wrong_shapes_is_a_clear_history_error(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    # Before: from_dict() accepted any JSON value for these dict fields, and code that reads
    # entry.details.get(...) later crashed with AttributeError ("internal error").
    _write_history(tmp_path, {"action": "add_law", "actor": "agent:test", **bad})
    with pytest.raises(HistoryError, match="line 1 is not a valid history entry"):
        history.read(tmp_path)


def test_history_entry_with_dict_fields_still_reads(tmp_path: Path) -> None:
    _write_history(
        tmp_path,
        {"action": "add_law", "actor": "agent:test", "details": {"a": 1}, "files": {}},
    )
    (entry,) = history.read(tmp_path)
    assert isinstance(entry, HistoryEntry) and entry.details == {"a": 1}


def test_as_dict_and_as_list_never_raise() -> None:
    assert as_dict({"a": 1}) == {"a": 1}
    assert as_dict(["a"]) == {} and as_dict(None) == {} and as_dict("x") == {}
    assert as_list([1, 2]) == [1, 2]
    assert as_list({"a": 1}) == [] and as_list("ab") == [] and as_list(None) == []


def test_cli_debug_mode_without_load_error_does_not_raise_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A command module missing without a recorded error must give result code 3, not
    # "TypeError: exceptions must derive from BaseException" (raise None) under LAWKEEP_DEBUG.
    monkeypatch.setenv("LAWKEEP_DEBUG", "1")
    monkeypatch.setattr(cli, "_load", lambda name: (None, None))
    assert cli.main(["check", "--json"]) == 3
