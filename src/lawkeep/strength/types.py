"""Shared data shapes of the strength package (re-exported by ``lawkeep.strength``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlantedBug:
    id: str  # stable for the same source, e.g. "src/billing/refunds.py::refund#3"
    function: str  # "billing.refunds:refund"
    file: str  # relative path
    line: int
    description: str  # plain English, e.g. "changed `<=` to `<` in `a <= b`"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def plain(self) -> str:
        return f"line {self.line}: {self.description}"


@dataclass
class CatchMatrix:
    bugs: list[PlantedBug] = field(default_factory=list["PlantedBug"])
    caught: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )  # law_id -> ids of bugs it caught
    notes: list[str] = field(default_factory=list[str])
    #: law_id -> ids of the bugs it was run against (laws only run against bugs planted in
    #: functions they execute). Added by C next to the contract fields.
    tested: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: function name -> law ids that execute it (from the coverage map). Added by C.
    laws_by_function: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    #: law ids that fail on today's code (their catches cannot be measured). Added by C.
    failing_laws: list[str] = field(default_factory=list[str])

    def caught_by_any(self) -> set[str]:
        out: set[str] = set()
        for ids in self.caught.values():
            out.update(ids)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "bugs": [b.to_dict() for b in self.bugs],
            "caught": self.caught,
            "notes": self.notes,
            "tested": self.tested,
            "laws_by_function": self.laws_by_function,
            "failing_laws": self.failing_laws,
        }
