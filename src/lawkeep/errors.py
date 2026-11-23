"""Exceptions shared across lawkeep.

Commands raise :class:`SetupError` (or a subclass) for anything that stops lawkeep from
running. ``lawkeep.cli`` turns it into a plain-English message and result code 3.
"""

from __future__ import annotations

from lawkeep.exitcodes import SETUP_PROBLEM


class LawkeepError(Exception):
    """Base class for lawkeep errors. ``message`` is plain English, ``hint`` says what to do."""

    exit_code: int = SETUP_PROBLEM

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n{self.hint}"
        return self.message


class SetupError(LawkeepError):
    """lawkeep cannot run: no project, bad config, missing tool. Result code 3."""


class ProjectNotFound(SetupError):
    """No folder with ``lawkeep.toml`` or ``laws/`` was found."""


class ConfigError(SetupError):
    """``lawkeep.toml`` is not valid TOML or has a bad value."""


class LockError(SetupError):
    """``laws.lock`` exists but cannot be read."""


class HistoryError(SetupError):
    """``laws/history.jsonl`` has a line that is not a valid history entry."""
