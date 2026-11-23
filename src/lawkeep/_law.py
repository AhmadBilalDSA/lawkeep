"""The ``@law`` decorator.

A law is a pytest test with one plain-English sentence::

    from lawkeep import law, given, st

    @law("A refund never exceeds the original charge")
    @given(charge=st.integers(min_value=0), amount=st.integers(min_value=0))
    def refund_is_capped(charge, amount):
        assert refund(charge, amount) <= charge

``inputs`` (optional) says in plain words what each input is, for the example tables people
read when they approve a law::

    @law("Combining result codes picks the most important one",
         inputs={"codes": "the result numbers being combined"})

``@law`` works above or below ``@given``, on plain functions (an L1 example law), and on
Hypothesis ``RuleBasedStateMachine`` classes (then expose ``MyMachine.TestCase`` in the laws
file as usual; it carries the same law information).

The decorator does not change how the test runs. It only attaches a :class:`LawMeta`
(attribute ``__lawkeep_law__``) and a pytest marker ``@pytest.mark.law(sentence, law_id=...)``.

Law ids
-------
A law id is stable and readable: the file's path inside ``laws/`` (dots, no ``.py``) plus the
function or class name, e.g. ``laws/billing/refunds.py::refund_is_capped`` has id
``billing.refunds.refund_is_capped``. A leading ``drafts`` folder is dropped, so a draft keeps
its id when it is approved and moved into ``laws/``. ``@law("...", id="R7")`` overrides it.
Static discovery (``lawkeep.discovery``) computes the same ids without importing the code.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from hypothesis.stateful import RuleBasedStateMachine

T = TypeVar("T")

LAW_ATTR = "__lawkeep_law__"
MARKER_NAME = "law"


@dataclass(frozen=True)
class LawMeta:
    """What ``@law`` attaches to a law."""

    sentence: str
    law_id: str
    qualname: str
    file: str | None  # absolute source file, if known
    kind: str  # "function" | "state_machine" | "class"
    #: plain-English description of each input, from ``@law(..., inputs={...})``
    inputs: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        if d.get("inputs") is None:
            d.pop("inputs", None)
        return d


def make_law_id(
    qualname: str,
    *,
    file: str | os.PathLike[str] | None,
    laws_dir: str | os.PathLike[str] | None,
    module: str | None = None,
) -> str:
    """Compute a law id (see module docstring).

    ``laws_dir`` is the project's ``laws/`` folder. When ``file`` is not inside it, the id
    falls back to ``<module>.<qualname>`` (or just ``qualname``).
    """
    if file is not None and laws_dir is not None:
        try:
            rel = Path(file).resolve().relative_to(Path(laws_dir).resolve())
        except ValueError:
            rel = None
        if rel is not None:
            parts = list(rel.with_suffix("").parts)
            if parts and parts[0] == "drafts":
                parts = parts[1:]
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            return ".".join([*parts, qualname])
    if module:
        return f"{module}.{qualname}"
    return qualname


def _laws_dir_for(file: str | None) -> Path | None:
    """Find the ``laws/`` folder that ``file`` belongs to, without importing anything."""
    if not file:
        return None
    from lawkeep.paths import LAWS_DIRNAME, find_root

    path = Path(file).resolve()
    root = find_root(path.parent)
    if root is not None:
        candidate = root / LAWS_DIRNAME
        try:
            path.relative_to(candidate.resolve())
            return candidate
        except ValueError:
            pass
    for parent in path.parents:
        if parent.name == LAWS_DIRNAME:
            return parent
    return None


def _innermost(obj: Any) -> Any:
    """Follow Hypothesis and functools wrappers down to the user's function."""
    seen: set[int] = set()
    while id(obj) not in seen:
        seen.add(id(obj))
        hyp = getattr(obj, "hypothesis", None)
        inner = getattr(hyp, "inner_test", None)
        if inner is not None:
            obj = inner
            continue
        wrapped = getattr(obj, "__wrapped__", None)
        if wrapped is not None:
            obj = wrapped
            continue
        break
    return obj


def _source_file(obj: Any) -> str | None:
    try:
        f = inspect.getsourcefile(obj)
    except (TypeError, OSError):
        return None
    return str(Path(f).resolve()) if f else None


def is_state_machine(cls: object) -> bool:
    try:
        from hypothesis.stateful import RuleBasedStateMachine
    except ImportError:  # pragma: no cover - hypothesis is a dependency
        return False
    return isinstance(cls, type) and issubclass(cls, RuleBasedStateMachine)


def _apply_marker(obj: Any, meta: LawMeta) -> None:
    try:
        import pytest
    except ImportError:  # pragma: no cover - pytest is a dependency
        return
    getattr(pytest.mark, MARKER_NAME)(meta.sentence, law_id=meta.law_id)(obj)


def _check_law_arguments(sentence: object, law_id: object, inputs: object) -> None:
    """Runtime checks for laws written by people and agents (the code may not be type-checked)."""
    if callable(sentence) and not isinstance(sentence, str):
        raise TypeError(
            "@law needs a plain-English sentence, for example: "
            '@law("A refund never exceeds the original charge")'
        )
    if not isinstance(sentence, str) or not sentence.strip():
        raise TypeError(
            "@law needs a non-empty plain-English sentence, for example: "
            '@law("A refund never exceeds the original charge")'
        )
    if law_id is not None and (not isinstance(law_id, str) or not law_id.strip()):
        raise TypeError('@law(id=...) must be a non-empty string, for example id="R7"')
    if inputs is not None and (
        not isinstance(inputs, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in cast("dict[object, object]", inputs).items()
        )
    ):
        raise TypeError(
            "@law(inputs=...) must map input names to plain-English descriptions, for example "
            'inputs={"amount": "the amount the customer asks to get back"}'
        )


def law(
    sentence: str, *, id: str | None = None, inputs: dict[str, str] | None = None
) -> Callable[[T], T]:
    """Mark a pytest test (or a Hypothesis state machine) as a law with a plain sentence.

    ``inputs`` maps each input name to a short plain-English description
    (``{"codes": "the result numbers being combined"}``); people see it next to the examples.
    """
    _check_law_arguments(sentence, id, inputs)
    sentence = " ".join(sentence.split())
    described = {k: " ".join(v.split()) for k, v in inputs.items()} if inputs else None

    def decorate(obj: T) -> T:
        inner = _innermost(obj)
        if isinstance(obj, type):
            kind = "state_machine" if is_state_machine(obj) else "class"
        elif callable(obj):
            kind = "function"
        else:
            raise TypeError(f"@law can only decorate a function or class, not {obj!r}")
        qualname = getattr(inner, "__qualname__", getattr(obj, "__qualname__", repr(obj)))
        file = _source_file(inner)
        law_id = id or make_law_id(
            qualname,
            file=file,
            laws_dir=_laws_dir_for(file),
            module=getattr(inner, "__module__", None),
        )
        meta = LawMeta(
            sentence=sentence,
            law_id=law_id,
            qualname=qualname,
            file=file,
            kind=kind,
            inputs=described,
        )
        setattr(obj, LAW_ATTR, meta)
        if inner is not obj:
            try:
                setattr(inner, LAW_ATTR, meta)
            except (AttributeError, TypeError):  # pragma: no cover
                pass
        if kind == "state_machine":
            machine = cast("type[RuleBasedStateMachine]", obj)
            test_case = machine.TestCase  # cached per class by Hypothesis
            setattr(test_case, LAW_ATTR, meta)
            _apply_marker(test_case, meta)
        else:
            _apply_marker(obj, meta)
        return obj

    return decorate


def get_law(obj: Any) -> LawMeta | None:
    """Return the :class:`LawMeta` of a law (function, wrapper, machine or its TestCase)."""
    meta = getattr(obj, LAW_ATTR, None)
    if isinstance(meta, LawMeta):
        return meta
    inner = _innermost(obj)
    meta = getattr(inner, LAW_ATTR, None)
    if isinstance(meta, LawMeta):
        return meta
    # unittest TestCase of a state machine: runTest knows its machine class
    run_test = getattr(obj, "runTest", None)
    machine = getattr(run_test, "_hypothesis_state_machine_class", None)
    if machine is not None:
        meta = getattr(machine, LAW_ATTR, None)
        if isinstance(meta, LawMeta):
            return meta
    return None


def is_law(obj: Any) -> bool:
    return get_law(obj) is not None
