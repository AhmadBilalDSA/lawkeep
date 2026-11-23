"""Input generators for laws: everything in ``hypothesis.strategies`` plus a few helpers.

Use it as ``st``::

    from lawkeep import st

    st.integers(min_value=0)      # any Hypothesis strategy
    st.money()                    # Decimal amounts like Decimal("12.50")
    st.charges()                  # a generator defined in your project's laws/strategies.py

Project generators
------------------
Names that are not Hypothesis strategies or lawkeep helpers are looked up in the project's
``laws/strategies.py`` (found from ``LAWKEEP_ROOT`` or the current folder), so drafted laws can
write ``st.charges()`` after the law agent adds ``charges`` there. That file is part of the
laws and is fingerprinted like any other law file.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from types import ModuleType
from typing import Any

import hypothesis.strategies as _hypothesis_strategies
from hypothesis.strategies import *  # noqa: F403  # pyright: ignore[reportWildcardImportFromLibrary]  # re-exporting all of hypothesis.strategies as `st` is this module's purpose
from hypothesis.strategies import (
    SearchStrategy,
    builds,
    decimals,
    integers,
    just,
    sampled_from,
    text,
)

__all__ = ["money", "percentages", "non_empty_text", "ids"]
__all__ += _hypothesis_strategies.__all__  # a form type checkers understand


def money(
    min_value: int | float | str | Decimal = 0,
    max_value: int | float | str | Decimal = 1_000_000,
    *,
    places: int = 2,
) -> SearchStrategy[Decimal]:
    """Money amounts as ``Decimal`` with ``places`` decimals (default 0.00 to 1,000,000.00).

    Never NaN or infinity. Edge values (the minimum, the maximum, zero when in range, one cent)
    come up often because Hypothesis favours boundaries.
    """
    lo = Decimal(str(min_value)).quantize(Decimal(1).scaleb(-places))
    hi = Decimal(str(max_value)).quantize(Decimal(1).scaleb(-places))
    if lo > hi:
        raise ValueError(f"money(): min_value {lo} is larger than max_value {hi}")
    return decimals(
        min_value=lo, max_value=hi, places=places, allow_nan=False, allow_infinity=False
    )


def percentages(*, places: int = 2) -> SearchStrategy[Decimal]:
    """Percentages from 0 to 100 as ``Decimal`` (e.g. ``Decimal("2.50")``)."""
    return money(0, 100, places=places)


def non_empty_text(*, max_size: int = 50) -> SearchStrategy[str]:
    """Text with at least one non-space character."""
    return text(min_size=1, max_size=max_size).filter(lambda s: s.strip() != "")


def ids(*, prefix: str = "", max_value: int = 10_000) -> SearchStrategy[str]:
    """Readable string ids like ``"user-17"`` (``prefix`` + number)."""
    sep = "-" if prefix and not prefix.endswith("-") else ""

    def build(n: int) -> str:
        return f"{prefix}{sep}{n}"

    return builds(build, integers(min_value=1, max_value=max_value))


# Keep linters quiet about names imported only for re-export.
_ = (just, sampled_from)

_PROJECT_MODULE_NAME = "lawkeep_project_strategies"
_loading = False


def _project_strategies() -> ModuleType | None:
    """Import the project's ``laws/strategies.py`` once (or return ``None``)."""
    global _loading
    cached = sys.modules.get(_PROJECT_MODULE_NAME)
    if cached is not None:
        return cached
    if _loading:
        return None
    from lawkeep.paths import find_root, strategies_path

    root = find_root()
    if root is None:
        return None
    path = strategies_path(root)
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(_PROJECT_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover
        return None
    module = importlib.util.module_from_spec(spec)
    _loading = True
    try:
        sys.modules[_PROJECT_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_PROJECT_MODULE_NAME, None)
            raise
    finally:
        _loading = False
    return module


def reset_project_cache() -> None:
    """Forget the imported project strategies (tests, or after laws/strategies.py changed)."""
    sys.modules.pop(_PROJECT_MODULE_NAME, None)


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    module = _project_strategies()
    if module is not None and hasattr(module, name):
        return getattr(module, name)
    raise AttributeError(
        f"st.{name} is not a Hypothesis strategy, a lawkeep helper, or defined in your "
        f"project's laws/strategies.py"
    )
