"""Saved failing examples: every failing input ever found, replayed on every run.

Owner: workstream B (gate).

File: ``paths.ledger_path(root)`` = ``laws/failing_examples.jsonl``. Append-only JSONL: lines may
be added (by ``lawkeep check`` when it finds a new failure, or by an agent under the
``add_example`` permission) but never removed or edited. ``laws.lock`` stores the line count
and a hash of those lines, so ``lock.check_append_only`` (run by the integrity check) flags a
removed or edited line; the integrity check also compares with the git base when there is one.

Design
------
Hypothesis already knows how to replay a failure: it stores the *choice sequence* that
produced the failing input in an example database and tries it first on the next run. lawkeep
wraps that instead of re-implementing it:

- During a gate run, each law gets its own Hypothesis database (an in-memory database, see
  ``lawkeep.pytest_plugin``). Reading the law's main key returns every choice sequence saved
  for that law in this file, so Hypothesis replays them before generating new inputs.
- When a law fails, Hypothesis shrinks the failure and saves the smallest choice sequence in
  that database. The plugin reports sequences that are not yet in this file, and ``lawkeep
  check`` appends them here, together with the readable example and failure message.

Because entries are keyed by law id (not by Hypothesis's hash of the test's source code), a
saved failure keeps being replayed after the law's code is edited or reformatted. If a law's
inputs change shape, an old choice sequence simply produces a different (still valid) input
or is skipped by Hypothesis; it never makes a law fail on its own.

One line (keys sorted)::

    {"v": 1,
     "ts": "2026-11-21T10:00:00.123456Z",
     "law_id": "refunds.refund_is_capped",
     "found_by": "lawkeep",                         # or "agent:<name>", "person:<name>"
     "example": {"charge": "40", "amount": "50"},   # readable, for people (may be null)
     "choices": "<base64>",                         # Hypothesis choice sequence (may be null)
     "choices_format": "hypothesis-choices",        # how "choices" is encoded
     "hypothesis_version": "6.170.17",
     "failure": "AssertionError: assert 50 <= 40",  # first line of the failure (may be null)
     "profile": "pr"}                               # which check profile found it (may be null)

Entries without ``choices`` (for example written by hand) are shown but cannot be replayed
automatically; to replay a hand-written input, add it to the law with ``@example(...)``.
"""

from __future__ import annotations

import base64
import binascii
import importlib.metadata
import json
import os
from pathlib import Path
from typing import Any, cast

from lawkeep.models import utc_now_iso
from lawkeep.paths import ledger_path

LEDGER_VERSION = 1
CHOICES_FORMAT = "hypothesis-choices"


def _hypothesis_version() -> str | None:
    try:
        return importlib.metadata.version("hypothesis")
    except Exception:  # pragma: no cover - hypothesis is a dependency
        return None


def load_with_problems(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """All saved failing examples (oldest first) plus plain-English problems with bad lines."""
    return load_path(ledger_path(Path(root)))


def load_path(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Like :func:`load_with_problems`, for an explicit file path."""
    path = Path(path)
    if not path.is_file():
        return [], []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"Cannot read laws/failing_examples.jsonl: {exc}"]
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            problems.append(
                f"laws/failing_examples.jsonl line {n} is not valid JSON ({exc}); it was skipped."
            )
            continue
        if not isinstance(data, dict):
            problems.append(f"laws/failing_examples.jsonl line {n} has no law_id; it was skipped.")
            continue
        data = cast("dict[str, Any]", data)
        if not isinstance(data.get("law_id"), str):
            problems.append(f"laws/failing_examples.jsonl line {n} has no law_id; it was skipped.")
            continue
        entries.append(data)
    return entries, problems


def load(root: Path) -> list[dict[str, Any]]:
    """All saved failing examples, oldest first (malformed lines are skipped)."""
    return load_with_problems(root)[0]


def encode_choices(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode_choices(entry: dict[str, Any]) -> bytes | None:
    """The raw Hypothesis choice bytes of one entry, or None if it has none / is unreadable."""
    raw = entry.get("choices")
    if not isinstance(raw, str) or not raw:
        return None
    if entry.get("choices_format", CHOICES_FORMAT) != CHOICES_FORMAT:
        return None
    try:
        return base64.b64decode(raw.encode("ascii"), validate=True)
    except (binascii.Error, ValueError):
        return None


def replay_values(entries: list[dict[str, Any]]) -> dict[str, list[bytes]]:
    """``{law_id: [choice bytes, ...]}`` for every replayable entry, oldest first, no repeats."""
    out: dict[str, list[bytes]] = {}
    for e in entries:
        value = decode_choices(e)
        if value is None:
            continue
        bucket = out.setdefault(e["law_id"], [])
        if value not in bucket:
            bucket.append(value)
    return out


def count_for(entries: list[dict[str, Any]], law_id: str) -> int:
    return sum(1 for e in entries if e.get("law_id") == law_id)


def _ensure_trailing_newline(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        return
    with open(path, "rb+") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            f.write(b"\n")


def add(
    root: Path,
    law_id: str,
    example: dict[str, Any] | None,
    *,
    found_by: str = "lawkeep",
    choices: bytes | str | None = None,
    failure: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Append one saved failing example and return the stored line.

    ``choices`` is the Hypothesis choice sequence (raw bytes, or already base64-encoded).
    If an entry with the same law id and the same choices already exists, nothing is written
    and the existing entry is returned (so re-finding a saved failure does not grow the file).
    """
    if not isinstance(law_id, str) or not law_id:  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard for untyped callers
        raise ValueError("ledger.add needs a law id, for example 'refunds.refund_is_capped'")
    root = Path(root)
    encoded: str | None
    if isinstance(choices, bytes):
        encoded = encode_choices(choices)
    else:
        encoded = choices or None
    if encoded is not None:
        for existing in load(root):
            if existing.get("law_id") == law_id and existing.get("choices") == encoded:
                return existing
    entry: dict[str, Any] = {
        "v": LEDGER_VERSION,
        "ts": utc_now_iso(),
        "law_id": law_id,
        "found_by": found_by,
        "example": dict(example) if example else None,
        "choices": encoded,
        "choices_format": CHOICES_FORMAT if encoded else None,
        "hypothesis_version": _hypothesis_version(),
        "failure": failure,
        "profile": profile,
    }
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    _ensure_trailing_newline(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return entry
