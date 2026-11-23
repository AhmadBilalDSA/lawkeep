"""Result codes shared by every lawkeep command.

0  ALL_HOLD       every law holds (or, for non-check commands: the command did what was asked)
1  SOME_FAIL      at least one law fails
2  WAITING        waiting for a person's decision (a weakening change, an unrecorded law change)
3  SETUP_PROBLEM  lawkeep could not run (no project, bad lawkeep.toml, missing tool, crash)

When several outcomes apply, :func:`combine` picks one with the precedence
SETUP_PROBLEM > SOME_FAIL > WAITING > ALL_HOLD: a setup problem hides everything else,
failing laws must be fixed in the code before a pending decision matters.
"""

from __future__ import annotations

ALL_HOLD = 0
SOME_FAIL = 1
WAITING = 2
SETUP_PROBLEM = 3

#: The one-line verdict printed for each result code.
VERDICTS: dict[int, str] = {
    ALL_HOLD: "ALL LAWS HOLD",
    SOME_FAIL: "SOME LAWS FAIL",
    WAITING: "WAITING FOR YOUR DECISION",
    SETUP_PROBLEM: "SETUP PROBLEM",
}

_PRECEDENCE = (SETUP_PROBLEM, SOME_FAIL, WAITING, ALL_HOLD)


def combine(*codes: int) -> int:
    """Return the most important code among ``codes`` (ALL_HOLD when empty).

    >>> combine(ALL_HOLD, WAITING, SOME_FAIL)
    1
    """
    present = set(codes)
    for code in _PRECEDENCE:
        if code in present:
            return code
    unknown = present - set(_PRECEDENCE)
    if unknown:
        raise ValueError(f"unknown result code(s): {sorted(unknown)}")
    return ALL_HOLD


def verdict(code: int) -> str:
    """Plain-English verdict line for a result code."""
    try:
        return VERDICTS[code]
    except KeyError:
        raise ValueError(f"unknown result code: {code}") from None
