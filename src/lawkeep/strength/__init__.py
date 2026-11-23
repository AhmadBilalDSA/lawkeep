"""Law strength: quick check, planted bugs, vacuity, weak test data and gaps.

Owner: workstream C (strength & protection).

Contract (docs/ARCHITECTURE.md, "strength"):
- ``run_strength`` powers ``lawkeep strength``.
- ``run_quick_check`` is also called by D (``draft`` and ``approve``) on drafts.
- ``catch_matrix`` is also called by D (``direction``: old vs new law on the same planted bugs)
  and by ``protection``.

Submodules: ``sourcemap`` (functions per file, no code runs), ``mutants`` (planted bugs, ast
based), ``_worker`` (the ``strength`` mode of the law-running core,
``lawkeep.lawrun``), ``core`` (the steps), ``render`` (plain-English text and markdown).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypeVar, cast

from lawkeep.strength.core import catch_matrix, run_quick_check, run_strength
from lawkeep.strength.types import CatchMatrix, PlantedBug

_R = TypeVar("_R")


def result_for(
    results: Mapping[str, _R] | Iterable[_R], law_id: str, file: str | None = None
) -> _R | None:
    """Find the result for ``law_id`` in a list or dict of results keyed by law id.

    When two target files define the same law id (an approved law and a draft of it),
    results are keyed ``"<law_id>@<relative file>"``; pass ``file`` (root-relative) to pick
    the one from that file. Returns None when nothing matches.
    """
    by_key: Mapping[object, _R]
    if isinstance(results, dict):
        by_key = cast("dict[object, _R]", results)
    else:
        by_key = {getattr(r, "law_id", None): r for r in cast("Iterable[_R]", results)}
    if file is not None and f"{law_id}@{file}" in by_key:
        return by_key[f"{law_id}@{file}"]
    if law_id in by_key:
        return by_key[law_id]
    if file is None:
        matches = [v for k, v in by_key.items() if isinstance(k, str) and k.split("@")[0] == law_id]
        if len(matches) == 1:
            return matches[0]
    return None


__all__ = [
    "CatchMatrix",
    "PlantedBug",
    "catch_matrix",
    "result_for",
    "run_quick_check",
    "run_strength",
]
