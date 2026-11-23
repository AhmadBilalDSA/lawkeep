"""Data shapes shared by every lawkeep module, with ``to_dict()`` for JSON output.

Every top-level report (``CheckReport``, ``StrengthReport``, ``ProtectionReport``,
``LawChangeReport``, ``ProposalResult``, ``SimpleReport``) carries ``kind``,
``schema_version``, ``exit_code`` and a one-line plain-English ``summary``, so agents can
always read those four keys first. The JSON schemas are described in docs/ARCHITECTURE.md.

``from_dict()`` is the inverse of ``to_dict()`` (unknown keys are ignored), so a report
written by one process (for example the pytest subprocess) can be read by another. A field
declared as a list or a dict that holds another kind of value raises ``TypeError`` (so a
hand-edited ``laws/history.jsonl`` line is reported as invalid instead of crashing later).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
    from typing_extensions import Self

from lawkeep import exitcodes

SCHEMA_VERSION = 1

LAW_STATUSES = ("holds", "fails", "error", "skipped", "not_run")
LEVELS = ("L1", "L2", "L3", "L4")
DIRECTIONS = ("stricter", "same", "looser", "unclear")
HISTORY_STATUSES = ("applied", "waiting", "rejected")
PERMISSION_USED = ("auto", "ask", "none")
SEVERITIES = ("error", "warning", "info")
PROTECTION_STATUSES = ("protected", "partly", "not_protected")
CHANGE_KINDS = (
    "added",
    "updated",
    "removed",
    "waiting",
    "rejected",
    "unrecorded",
    "confirmed",  # a person confirmed an approval recorded without a terminal
    "undone",  # a person undid an approval recorded without a terminal
)
#: "unchanged": the proposed file equals the law as it is (nothing recorded);
#: "refused": lawkeep did not look at the proposal (e.g. laws changed outside lawkeep first).
PROPOSAL_DECISIONS = ("applied", "waiting", "rejected", "unchanged", "refused")

#: Recommended history actions. Other strings are accepted (not validated) so workstreams can
#: add actions without editing this file; document new ones in docs/ARCHITECTURE.md.
HISTORY_ACTIONS = (
    "init",  # lawkeep init created laws/ and the first lock
    "add_law",  # a new law entered laws/ (usually an approved draft)
    "add_example",  # an example or saved failing case was added to a law
    "rename_target",  # a law was updated after a rename, same meaning confirmed
    "change_law",  # what a law checks changed (direction in `direction`)
    "remove_law",  # a law was deleted
    "skip_law",  # a law was skipped/xfailed
    "change_settings",  # lawkeep.toml changed through lawkeep
    "reject_draft",  # a person rejected a draft
    "accept_unrecorded_change",  # a person accepted a change made outside lawkeep
    "relock",  # laws.lock rewritten without a law change (e.g. after a merge)
    "confirm_approval",  # a person confirmed approvals recorded without a terminal
    "undo_approval",  # a person undid approvals recorded without a terminal (files restored)
)


def utc_now_iso() -> str:
    """Current UTC time as ``2026-11-21T10:00:00.123456Z`` (sorts correctly as text)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    """Parse a timestamp written by :func:`utc_now_iso` (or any ISO 8601 form)."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _lawkeep_version() -> str:
    from lawkeep import __version__

    return __version__


# ---------------------------------------------------------------------------------------------
# to_dict / from_dict machinery


def to_plain(value: Any) -> Any:
    """Convert reports, dataclasses, paths and datetimes into JSON-ready values."""
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(k): to_plain(v) for k, v in mapping.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [to_plain(v) for v in cast("Iterable[object]", value)]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__fspath__"):
        return str(value)
    return value


def _convert(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin in (typing.Union, types.UnionType):
        for arg in args:
            if arg is type(None):
                continue
            if isinstance(arg, type) and issubclass(arg, _Model) and isinstance(value, dict):
                return arg.from_dict(cast("dict[str, Any]", value))
        return value
    if origin is list and args:
        if not isinstance(value, list):
            raise TypeError(f"expected a list, got {type(value).__name__}")
        return [_convert(args[0], v) for v in cast("list[Any]", value)]
    if origin is dict and len(args) == 2:
        if not isinstance(value, dict):
            raise TypeError(f"expected an object, got {type(value).__name__}")
        return {k: _convert(args[1], v) for k, v in cast("dict[str, Any]", value).items()}
    if isinstance(hint, type) and issubclass(hint, _Model) and isinstance(value, dict):
        return hint.from_dict(cast("dict[str, Any]", value))
    return value


def _fields(obj: object) -> tuple[dataclasses.Field[Any], ...]:
    """``dataclasses.fields`` for the ``_Model`` mixin (every subclass is a dataclass)."""
    return dataclasses.fields(cast("DataclassInstance", obj))


class _Model:
    """Mixin: ``to_dict()`` and ``from_dict()`` for dataclasses."""

    def to_dict(self) -> dict[str, Any]:
        return {f.name: to_plain(getattr(self, f.name)) for f in _fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        hints = typing.get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for f in _fields(cls):
            if f.name in data:
                kwargs[f.name] = _convert(hints.get(f.name, Any), data[f.name])
        return cls(**kwargs)


def as_dict(value: object) -> dict[str, Any]:
    """``value`` if it is a dict, else ``{}``.

    For JSON values whose shape nothing has checked yet, e.g. a key inside
    ``HistoryEntry.details`` (``from_dict`` checks only the declared list/dict fields).
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    """``value`` if it is a list, else ``[]`` (same purpose as :func:`as_dict`)."""
    return cast("list[object]", value) if isinstance(value, list) else []


def _check_choice(name: str, value: Any, choices: tuple[str, ...], *, optional: bool = False):
    if value is None and optional:
        return
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)} (got {value!r})")


# ---------------------------------------------------------------------------------------------
# Laws and gate results


@dataclass
class LawInfo(_Model):
    """A law found in ``laws/`` (statically by lawkeep.discovery, or at runtime by the plugin)."""

    law_id: str
    sentence: str | None  # None when the sentence is not a plain string literal
    file: str  # relative to project root, forward slashes: "laws/refunds.py"
    qualname: str  # "refund_is_capped" or "RefundMachine"
    line: int = 0
    kind: str = "function"  # "function" | "state_machine" | "class"
    source_hash: str | None = None  # "sha256:<hex>" of the law's source incl. decorators
    is_draft: bool = False
    explicit_id: bool = False  # True when written as @law("...", id="R7")


@dataclass
class LawResult(_Model):
    """The result of running one law once."""

    law_id: str
    sentence: str | None
    status: str  # one of LAW_STATUSES
    level: str = "L2"  # one of LEVELS; L1 = fixed examples only, L3 = CrossHair backend
    examples_tried: int = 0
    #: argument name -> repr of the value, e.g. {"charge": "Charge(total=40)", "amount": "50"}.
    #: When the arguments cannot be split, {"call": "<the falsifying call as text>"}.
    simplest_failing_example: dict[str, str] | None = None
    failure_message: str | None = None  # the assertion / error message, plain text
    duration_seconds: float = 0.0
    #: Free-form evidence: {"seed": 0, "backend": "hypothesis", "saved_failures_replayed": 2,
    #: "profile": "pr", "explicit_examples": 1}. Keys are documented in ARCHITECTURE.md.
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])
    file: str | None = None
    nodeid: str | None = None

    def __post_init__(self) -> None:
        _check_choice("LawResult.status", self.status, LAW_STATUSES)
        _check_choice("LawResult.level", self.level, LEVELS)

    @property
    def holds(self) -> bool:
        return self.status == "holds"


@dataclass
class IntegrityIssue(_Model):
    """Something the integrity check flags: an unrecorded law change or a side-door file.

    ``severity="error"`` means a person must decide (the gate returns at least WAITING);
    ``"warning"`` and ``"info"`` are shown but do not change the result code.
    """

    kind: str  # e.g. "unrecorded_law_change", "side_door_file" (list in ARCHITECTURE.md)
    message: str  # plain English, says what happened and what to do
    path: str | None = None
    severity: str = "error"
    details: dict[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        _check_choice("IntegrityIssue.severity", self.severity, SEVERITIES)


@dataclass
class ExampleRow(_Model):
    """One concrete example shown to a person: input -> result -> does the law hold?"""

    inputs: dict[str, str]  # argument name -> readable value
    result: str | None = None  # what the real code returned (readable), or the error
    holds: bool | None = None  # does the (new) law hold on this input?
    old_holds: bool | None = None  # for proposals: did the old version hold?
    note: str | None = None  # e.g. "edge case: zero"


@dataclass
class HistoryEntry(_Model):
    """One line of ``laws/history.jsonl``."""

    action: str  # see HISTORY_ACTIONS
    actor: str  # "agent:claude-code", "person:Ada", "lawkeep"
    law_id: str | None = None
    direction: str | None = None  # one of DIRECTIONS; None for non-law actions
    reason: str = ""
    permission_used: str = "none"  # "auto" | "ask" | "none"
    approved_by: str | None = None  # "person:Ada" when a person said yes
    status: str = "applied"  # "applied" | "waiting" | "rejected"
    sentence_before: str | None = None
    sentence_after: str | None = None
    #: Files this change touched -> fingerprint AFTER the change ("sha256:..."), None = deleted.
    #: Filled by history.record_change(); used to tell recorded changes from unrecorded ones.
    files: dict[str, str | None] = field(default_factory=dict[str, str | None])
    details: dict[str, Any] = field(default_factory=dict[str, Any])
    timestamp: str = field(default_factory=utc_now_iso)
    id: str = ""  # short unique id, e.g. "h-3f9a1c2b"; set by history.append() when empty
    refers_to: str | None = None  # id of the "waiting" entry this entry decides
    v: int = 1

    def __post_init__(self) -> None:
        _check_choice("HistoryEntry.direction", self.direction, DIRECTIONS, optional=True)
        _check_choice("HistoryEntry.status", self.status, HISTORY_STATUSES)
        _check_choice("HistoryEntry.permission_used", self.permission_used, PERMISSION_USED)
        if not self.action:
            raise ValueError("HistoryEntry.action must not be empty")
        if not self.actor:
            raise ValueError("HistoryEntry.actor must not be empty")


# ---------------------------------------------------------------------------------------------
# Reports


@dataclass(kw_only=True)
class Report(_Model):
    """Base of every top-level report."""

    KIND: ClassVar[str] = "report"
    kind: str = ""
    schema_version: int = SCHEMA_VERSION
    exit_code: int = exitcodes.ALL_HOLD
    summary: str = ""
    lawkeep_version: str = field(default_factory=_lawkeep_version)

    def __post_init__(self) -> None:
        if not self.kind:
            self.kind = self.KIND

    @property
    def verdict(self) -> str:
        return exitcodes.verdict(self.exit_code)

    def to_text(self) -> str:
        return self.summary or self.verdict


@dataclass(kw_only=True)
class CheckReport(Report):
    KIND: ClassVar[str] = "check"
    profile: str = "pr"
    root: str = ""
    laws: list[LawResult] = field(default_factory=list["LawResult"])
    integrity: list[IntegrityIssue] = field(default_factory=list["IntegrityIssue"])
    waiting: list[LawChange] = field(
        default_factory=list["LawChange"]
    )  # pending decisions, if known
    notes: list[str] = field(default_factory=list[str])
    started_at: str = field(default_factory=utc_now_iso)
    duration_seconds: float = 0.0
    #: Law ids the run was limited to (``--law``); empty = every law was run.
    only_laws: list[str] = field(default_factory=list[str])
    #: How many approved laws exist (so a partial run can say what it left out).
    laws_known: int | None = None
    #: Everything waiting for a person (``waiting.WaitingList.to_dict``): count, ids, by kind.
    #: Additive; ``waiting`` above keeps its meaning (pending proposals only).
    waiting_for_person: dict[str, Any] = field(default_factory=dict[str, Any])
    #: lawkeep's guess whether a person is there to answer (``presence.detect``).
    person_present: bool | None = None
    person_present_reason: str | None = None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in LAW_STATUSES}
        for r in self.laws:
            out[r.status] += 1
        return out

    def finalize(self) -> CheckReport:
        """Set ``exit_code`` and ``summary`` from the contents. Returns self.

        fails/error laws -> SOME_FAIL; integrity errors or waiting items -> WAITING;
        otherwise ALL_HOLD. (Setup problems are raised as SetupError before a report exists.)
        """
        c = self.counts()
        codes = [exitcodes.ALL_HOLD]
        if c["fails"] or c["error"]:
            codes.append(exitcodes.SOME_FAIL)
        if any(i.severity == "error" for i in self.integrity) or self.waiting:
            codes.append(exitcodes.WAITING)
        self.exit_code = exitcodes.combine(*codes)
        bad = c["fails"] + c["error"]
        total = len(self.laws)
        if self.exit_code == exitcodes.SOME_FAIL:
            self.summary = f"SOME LAWS FAIL: {bad} of {total} laws fail."
        elif self.exit_code == exitcodes.WAITING:
            n = sum(1 for i in self.integrity if i.severity == "error") + len(self.waiting)
            self.summary = (
                f"WAITING FOR YOUR DECISION: {total - bad} of {total} laws hold, but {n} "
                f"item(s) need a person's decision."
            )
        elif total == 0:
            self.summary = "ALL LAWS HOLD: there are no laws yet (nothing was checked)."
        else:
            self.summary = f"ALL LAWS HOLD: {total} of {total} laws hold."
        return self

    def to_text(self) -> str:
        from lawkeep.output import plain_example

        lines = [f"lawkeep check · profile {self.profile}"]
        for r in self.laws:
            mark = {"holds": "ok  ", "fails": "FAIL", "error": "ERR ", "skipped": "SKIP"}.get(
                r.status, "----"
            )
            lines.append(f'  {mark} {r.law_id}  "{r.sentence or ""}"')
            lines.append(
                f"       {r.level} · {r.examples_tried} examples · {r.duration_seconds:.2f}s"
            )
            if r.simplest_failing_example:
                lines.append(
                    f"       simplest failing example: {plain_example(r.simplest_failing_example)}"
                )
            if r.failure_message and r.status != "holds":
                first = (
                    r.failure_message.strip().splitlines()[0] if r.failure_message.strip() else ""
                )
                lines.append(f"       why: {first}")
        for i in self.integrity:
            label = {"error": "!", "warning": "?", "info": "i"}[i.severity]
            where = f" ({i.path})" if i.path else ""
            lines.append(f"  {label} {i.message}{where}")
        for w in self.waiting:
            lines.append(f"  ! WAITING {w.law_id}: {w.reason or w.sentence or ''}")
        for n in self.notes:
            lines.append(f"  note: {n}")
        lines.append(self.summary or self.verdict)
        return "\n".join(lines)


@dataclass
class QuickCheckOutcome(_Model):
    """One dumb replacement tried by the quick strength check."""

    function: str  # "billing.refunds:refund"
    variant: str  # "always_zero" | "identity" | "raises" | "always_none" | ...
    caught: bool  # True when the law failed with the dumb version (good)
    note: str | None = None


@dataclass
class StrengthResult(_Model):
    """How strong one law is."""

    law_id: str
    sentence: str | None = None
    functions_touched: list[str] = field(default_factory=list[str])  # "module:qualname"
    quick_check: list[QuickCheckOutcome] = field(default_factory=list["QuickCheckOutcome"])
    planted_bugs_total: int | None = None  # None = mutation run not done
    planted_bugs_caught: int | None = None
    missed_bugs: list[dict[str, Any]] = field(
        default_factory=list[dict[str, Any]]
    )  # {file, line, description}
    vacuous: bool = False
    vacuity_reason: str | None = None
    weak_data: list[str] = field(default_factory=list[str])  # plain-English notes
    flags: list[str] = field(default_factory=list[str])  # plain-English problems; empty = fine

    @property
    def mutation_score(self) -> float | None:
        if not self.planted_bugs_total or self.planted_bugs_caught is None:
            return None
        return self.planted_bugs_caught / self.planted_bugs_total

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["mutation_score"] = self.mutation_score
        return d


@dataclass(kw_only=True)
class StrengthReport(Report):
    KIND: ClassVar[str] = "strength"
    mode: str = "quick"  # "quick" | "full"
    laws: list[StrengthResult] = field(default_factory=list["StrengthResult"])
    uncovered_functions: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])


@dataclass
class ProtectionResult(_Model):
    """Is one changed function protected by laws?"""

    function: str  # "billing.refunds:refund"
    file: str  # relative path
    status: str  # "protected" | "partly" | "not_protected"
    changed_lines: list[int] = field(default_factory=list[int])
    planted_bugs_total: int = 0
    planted_bugs_caught: int = 0
    laws: list[str] = field(default_factory=list[str])  # law ids that touch it
    note: str | None = None

    def __post_init__(self) -> None:
        _check_choice("ProtectionResult.status", self.status, PROTECTION_STATUSES)


@dataclass(kw_only=True)
class ProtectionReport(Report):
    KIND: ClassVar[str] = "protection"
    base_ref: str | None = None
    functions: list[ProtectionResult] = field(default_factory=list["ProtectionResult"])
    notes: list[str] = field(default_factory=list[str])


@dataclass
class LawChange(_Model):
    """One row of the law change report (e.g. ``+ ADDED R7 "A refund is never negative"``)."""

    change: str  # one of CHANGE_KINDS
    law_id: str | None
    sentence: str | None = None  # sentence now (or proposed)
    sentence_before: str | None = None
    direction: str | None = None
    permission_used: str | None = None
    approved_by: str | None = None
    actor: str | None = None
    reason: str | None = None
    evidence: str | None = None  # "catches 4 of 4 planted bugs", "same meaning, confirmed"
    examples: list[ExampleRow] = field(
        default_factory=list["ExampleRow"]
    )  # e.g. "now allowed: ..."
    timestamp: str | None = None
    history_id: str | None = None

    def __post_init__(self) -> None:
        _check_choice("LawChange.change", self.change, CHANGE_KINDS)
        _check_choice("LawChange.direction", self.direction, DIRECTIONS, optional=True)

    @classmethod
    def from_history(cls, entry: HistoryEntry) -> LawChange:
        """Turn a history entry into a report row (no evidence/examples; callers add those)."""
        if entry.status == "waiting":
            change = "waiting"
        elif entry.status == "rejected":
            change = "rejected"
        elif entry.action in ("add_law", "init"):
            change = "added"
        elif entry.action == "remove_law":
            change = "removed"
        elif entry.action == "accept_unrecorded_change":
            change = "unrecorded"
        elif entry.action == "confirm_approval":
            change = "confirmed"
        elif entry.action == "undo_approval":
            change = "undone"
        else:
            change = "updated"
        evidence = as_dict(entry.details).get("evidence")
        return cls(
            change=change,
            law_id=entry.law_id,
            sentence=entry.sentence_after or entry.sentence_before,
            sentence_before=entry.sentence_before if entry.sentence_after else None,
            direction=entry.direction,
            permission_used=entry.permission_used,
            approved_by=entry.approved_by,
            actor=entry.actor,
            reason=entry.reason or None,
            evidence=evidence if isinstance(evidence, str) else None,
            timestamp=entry.timestamp,
            history_id=entry.id or None,
        )


@dataclass(kw_only=True)
class LawChangeReport(Report):
    KIND: ClassVar[str] = "report"
    since: str | None = None
    changes: list[LawChange] = field(default_factory=list["LawChange"])
    integrity: list[IntegrityIssue] = field(default_factory=list["IntegrityIssue"])
    protection: ProtectionReport | None = None
    notes: list[str] = field(default_factory=list[str])
    #: See CheckReport: everything waiting for a person, and whether one seems to be there.
    waiting_for_person: dict[str, Any] = field(default_factory=dict[str, Any])
    person_present: bool | None = None
    person_present_reason: str | None = None


@dataclass(kw_only=True)
class ProposalResult(Report):
    KIND: ClassVar[str] = "propose"
    law_id: str = ""
    direction: str = "unclear"
    decision: str = "waiting"  # "applied" | "waiting" | "rejected" | "unchanged" | "refused"
    permission_key: str | None = None  # e.g. "weaken_law"
    permission_value: str | None = None  # "auto" | "ask"
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])
    examples: list[ExampleRow] = field(default_factory=list["ExampleRow"])
    history_id: str | None = None
    #: lawkeep's guess whether a person is there (``presence.detect``), and what the agent
    #: should do next ("Parked as h-...: continue ..." when nobody is there).
    person_present: bool | None = None
    next_step: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_choice("ProposalResult.direction", self.direction, DIRECTIONS)
        _check_choice("ProposalResult.decision", self.decision, PROPOSAL_DECISIONS)


@dataclass(kw_only=True)
class SimpleReport(Report):
    """Generic report for commands without a special shape (init, draft, approve, hook, errors).

    ``kind`` is the command name. ``lines`` are printed as-is in text mode.
    """

    KIND: ClassVar[str] = "simple"
    lines: list[str] = field(default_factory=list[str])
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    hint: str | None = None

    def to_text(self) -> str:
        out = list(self.lines)
        if self.summary:
            out.append(self.summary)
        if self.hint:
            out.append(self.hint)
        return "\n".join(out) if out else self.verdict
