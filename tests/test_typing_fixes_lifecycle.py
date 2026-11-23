"""Fixes found while making the law lifecycle modules pass pyright strict (2026-11-23)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lawkeep import _probe_worker
from lawkeep._probe_worker import _module_from_file


def test_module_from_file_imports_a_python_file(tmp_path: Path) -> None:
    f = tmp_path / "made_up_mod.py"
    f.write_text("VALUE = 41 + 1\n", encoding="utf-8")
    try:
        mod = _module_from_file("lawkeep_test_made_up_mod", str(f))
        assert mod.VALUE == 42
        assert sys.modules["lawkeep_test_made_up_mod"] is mod
    finally:
        sys.modules.pop("lawkeep_test_made_up_mod", None)


def test_module_from_file_without_spec_raises_import_error(tmp_path: Path) -> None:
    # spec_from_file_location() returns None for a file it has no loader for; before the fix
    # this surfaced as "AttributeError: 'NoneType' object has no attribute 'loader'".
    f = tmp_path / "notes.txt"
    f.write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(ImportError, match="cannot import"):
        _module_from_file("lawkeep_test_notes", str(f))


def test_readable_collections_unchanged() -> None:
    assert _probe_worker.readable([1, 2]) == "[1, 2]"
    assert _probe_worker.readable((1,)) == "(1,)"
    assert _probe_worker.readable({"a": 1}) == "{'a': 1}"
    assert _probe_worker.readable(set()) == "set()"
    assert _probe_worker.readable(list(range(10))).endswith(", ...]")
