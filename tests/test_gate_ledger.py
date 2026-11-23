"""Gate (B): saved failing examples (laws/failing_examples.jsonl)."""

from __future__ import annotations

import json

from lawkeep import ledger, paths


def _setup(tmp_path):
    (tmp_path / "laws").mkdir()
    return tmp_path


def test_load_missing_file_is_empty(tmp_path):
    assert ledger.load(_setup(tmp_path)) == []


def test_add_and_load_roundtrip(tmp_path):
    root = _setup(tmp_path)
    entry = ledger.add(
        root,
        "refunds.refund_is_capped",
        {"charge": "0", "amount": "1001"},
        choices=b"\x01\x02",
        failure="AssertionError: assert 1001 <= 0",
        profile="pr",
    )
    assert entry["law_id"] == "refunds.refund_is_capped"
    assert entry["found_by"] == "lawkeep"
    assert entry["ts"].endswith("Z")
    loaded = ledger.load(root)
    assert loaded == [entry]
    assert ledger.decode_choices(loaded[0]) == b"\x01\x02"
    # one JSON object per line, keys sorted, newline-terminated
    text = paths.ledger_path(root).read_text()
    assert text.endswith("\n") and len(text.splitlines()) == 1
    line = json.loads(text)
    assert list(line) == sorted(line)


def test_add_same_choices_twice_does_not_grow_the_file(tmp_path):
    root = _setup(tmp_path)
    ledger.add(root, "a.law", {"x": "1"}, choices=b"\x05")
    ledger.add(root, "a.law", {"x": "1"}, choices=b"\x05")
    ledger.add(root, "b.law", {"x": "1"}, choices=b"\x05")  # other law: kept
    assert len(ledger.load(root)) == 2


def test_add_only_appends(tmp_path):
    root = _setup(tmp_path)
    ledger.add(root, "a.law", None, choices=b"\x01")
    first = paths.ledger_path(root).read_bytes()
    ledger.add(root, "a.law", None, choices=b"\x02")
    assert paths.ledger_path(root).read_bytes().startswith(first)


def test_add_repairs_missing_trailing_newline(tmp_path):
    root = _setup(tmp_path)
    paths.ledger_path(root).write_text('{"law_id": "a.law", "ts": "x"}')  # no newline
    ledger.add(root, "b.law", None)
    assert [e["law_id"] for e in ledger.load(root)] == ["a.law", "b.law"]


def test_bad_lines_are_skipped_with_a_problem(tmp_path):
    root = _setup(tmp_path)
    paths.ledger_path(root).write_text('not json\n{"no_law": 1}\n{"law_id": "a.law"}\n')
    entries, problems = ledger.load_with_problems(root)
    assert [e["law_id"] for e in entries] == ["a.law"]
    assert len(problems) == 2 and "line 1" in problems[0]


def test_replay_values_groups_by_law_and_skips_unreplayable(tmp_path):
    root = _setup(tmp_path)
    ledger.add(root, "a.law", None, choices=b"\x01")
    ledger.add(root, "a.law", {"x": "hand written"})  # no choices: shown, not replayed
    ledger.add(root, "b.law", None, choices=b"\x02")
    with open(paths.ledger_path(root), "a") as f:
        f.write(json.dumps({"law_id": "a.law", "choices": "%%%not base64"}) + "\n")
    values = ledger.replay_values(ledger.load(root))
    assert values == {"a.law": [b"\x01"], "b.law": [b"\x02"]}
    assert ledger.count_for(ledger.load(root), "a.law") == 3


def test_add_accepts_base64_choices(tmp_path):
    root = _setup(tmp_path)
    e = ledger.add(root, "a.law", None, choices=ledger.encode_choices(b"\x09"))
    assert ledger.decode_choices(e) == b"\x09"
