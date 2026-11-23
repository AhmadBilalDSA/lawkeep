"""lawkeep: laws for AI-assisted coding.

Public API for law files::

    from lawkeep import law, given, st, example, settings

- ``law``      the ``@law("plain English sentence")`` decorator (lawkeep)
- ``given``    ``hypothesis.given``
- ``st``       ``hypothesis.strategies`` plus helpers such as ``st.money()`` (lawkeep.strategies)
- ``example``  ``hypothesis.example`` (a fixed example that always runs)
- ``settings`` ``hypothesis.settings``
- ``assume``, ``note``, ``event`` from Hypothesis, and ``get_law`` / ``is_law`` / ``LawMeta``.

Imports are lazy so ``lawkeep`` CLI commands start fast and do not import Hypothesis or pytest
unless they need them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "law",
    "given",
    "st",
    "example",
    "settings",
    "assume",
    "note",
    "event",
    "get_law",
    "is_law",
    "LawMeta",
]

_LAZY: dict[str, tuple[str, str | None]] = {
    "law": ("lawkeep._law", "law"),
    "get_law": ("lawkeep._law", "get_law"),
    "is_law": ("lawkeep._law", "is_law"),
    "LawMeta": ("lawkeep._law", "LawMeta"),
    "st": ("lawkeep.strategies", None),
    "given": ("hypothesis", "given"),
    "example": ("hypothesis", "example"),
    "settings": ("hypothesis", "settings"),
    "assume": ("hypothesis", "assume"),
    "note": ("hypothesis", "note"),
    "event": ("hypothesis", "event"),
}

if TYPE_CHECKING:  # pragma: no cover
    from hypothesis import assume, event, example, given, note, settings

    from lawkeep import strategies as st
    from lawkeep._law import LawMeta, get_law, is_law, law


def __getattr__(name: str) -> Any:
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'lawkeep' has no attribute {name!r}") from None
    import importlib

    module = importlib.import_module(module_name)
    value = module if attr is None else getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
