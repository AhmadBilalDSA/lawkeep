"""Permissions: which `lawkeep.toml` key governs a law change, and whether it may go ahead.

Owner: workstream D (law lifecycle).

Mapping (PLAN.md "Permissions"; ARCHITECTURE.md section 6, D):

=====================================  ===============  ==================================
change                                 key              default
=====================================  ===============  ==================================
anything looser, delete, skip          weaken_law       ask (ALWAYS; cannot be "auto")
add an example / saved failing case    add_example      auto
new law, or an edit found stricter     add_law          auto
same meaning (e.g. after a rename)     rename_target    auto
unclear (mixed or cannot evaluate)     change_law       ask
=====================================  ===============  ==================================

``weaken_law`` is always "ask": config already forces it, and :func:`decide` forces it again
so a hand-built Config cannot bypass it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lawkeep.config import FIXED_ASK, Config

#: History actions that weaken a law whatever the evidence says.
WEAKENING_ACTIONS = ("remove_law", "skip_law")

_EXPLAIN = {
    ("weaken_law", "ask"): "Loosening, deleting or skipping a law always needs your yes.",
    ("add_law", "auto"): "Adding checks (a new law or a stricter one) is allowed; you are told.",
    ("add_law", "ask"): 'Your settings say new or stricter laws need your yes (add_law = "ask").',
    ("add_example", "auto"): "Adding an example or a saved failing case is allowed; you are told.",
    ("add_example", "ask"): 'Your settings say new examples need your yes (add_example = "ask").',
    ("rename_target", "auto"): (
        "Only names changed (a consistent rename) and lawkeep found no difference in "
        "results on the cases and broken versions it tried, so it is allowed; you are told. "
        "This is evidence from samples, not a proof."
    ),
    ("rename_target", "ask"): (
        'Your settings say same-meaning updates need your yes (rename_target = "ask").'
    ),
    ("change_law", "ask"): (
        "lawkeep could not tell whether this makes the law stricter or looser, so it needs "
        "your yes."
    ),
    ("change_law", "auto"): (
        'Your settings allow changes of unclear direction (change_law = "auto"); you are told.'
    ),
}


@dataclass
class PermissionDecision:
    key: str  # e.g. "weaken_law"
    value: str  # "auto" | "ask"
    may_apply: bool  # True only for "auto"
    explanation: str  # plain English, e.g. "Loosening a law always needs your yes."

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def permission_key(action: str, direction: str | None) -> str:
    """Map (history action, direction) to a permission key (table in the module docstring)."""
    if action in WEAKENING_ACTIONS or direction == "looser":
        return "weaken_law"
    if action == "add_example" and direction in ("stricter", "same", None):
        return "add_example"
    if direction == "stricter":
        return "add_law"
    if direction == "same":
        return "rename_target"
    return "change_law"


def decide(cfg: Config, action: str, direction: str | None) -> PermissionDecision:
    key = permission_key(action, direction)
    value = cfg.permissions.get(key)
    if key in FIXED_ASK:
        value = "ask"
    explanation = _EXPLAIN.get((key, value), f"Permission {key} = {value!r}.")
    return PermissionDecision(
        key=key, value=value, may_apply=value == "auto", explanation=explanation
    )
