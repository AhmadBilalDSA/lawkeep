"""The ``probe`` mode of the law-running core: a law run on varied inputs, input by input.

Runs inside the isolated child started by ``lawkeep.lawrun.run(..., "probe", ...)``
(``lawkeep._lawrun_child`` calls :func:`run`): same isolation rules as the gate, cwd = project
root, the project's source folders on ``sys.path``, ``LAWKEEP_ROOT`` set (so
``st.<project generator>()`` works), ``PYTHONHASHSEED=0``, private Hypothesis storage, no
bytecode written. No pytest: Hypothesis draws the inputs and a recorder runs the real law body
on each one (see ``lawkeep.examples_table`` for how the parent uses the results).

Must not import lawkeep commands. Sends one ``{"type": "probe", "result": ...}`` record.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Collection
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from lawkeep.lawrun import ResultsChannel


_VARIANTS = (
    ("zero", "always returns 0"),
    ("plus_one", "returns 1 more than it should"),
    ("minus_one", "returns 1 less than it should"),
    ("double", "returns double the right answer"),
    ("first_input", "returns its first input unchanged"),
    ("none", "returns nothing (None)"),
    ("raises", "always raises an error"),
    ("empty", "returns an empty value"),
    ("drop_last", "drops the last item"),
    ("flip", "returns the opposite true/false"),
)
_MUTANT_CASES = 30
_MAX_TEXT = 120


def readable(value: object, depth: int = 0) -> str:
    """Short, plain text for a value: ``Decimal('0.50')`` -> ``0.50``."""
    import dataclasses
    from decimal import Decimal
    from fractions import Fraction

    original: object = value  # not narrowed by the isinstance checks below
    try:
        if isinstance(value, (Decimal, Fraction)):
            text = str(value)
        elif isinstance(value, (bool, int, float)) or value is None:
            text = repr(value)
        elif isinstance(value, str):
            text = repr(value)
        elif dataclasses.is_dataclass(value) and not isinstance(value, type) and depth < 3:
            inner = ", ".join(
                f"{f.name}={readable(getattr(value, f.name), depth + 1)}"
                for f in dataclasses.fields(value)
            )
            text = f"{type(value).__name__}({inner})"
        elif isinstance(value, (list, tuple, set, frozenset)) and depth < 3:
            seq = cast("Collection[object]", value)
            items = [readable(v, depth + 1) for v in list(seq)[:8]]
            more = ", ..." if len(seq) > 8 else ""
            if isinstance(value, list):
                text = "[" + ", ".join(items) + more + "]"
            elif isinstance(value, tuple):
                text = "(" + ", ".join(items) + ("," if len(items) == 1 else "") + more + ")"
            else:
                text = "{" + ", ".join(items) + more + "}" if items else "set()"
        elif isinstance(value, dict) and depth < 3:
            mapping = cast("dict[object, object]", value)
            items = [
                f"{readable(k, depth + 1)}: {readable(v, depth + 1)}"
                for k, v in list(mapping.items())[:8]
            ]
            text = "{" + ", ".join(items) + (", ..." if len(mapping) > 8 else "") + "}"
        else:
            text = repr(original)
    except Exception:  # noqa: BLE001 - a broken __repr__ must not stop the probe
        text = f"<{type(original).__name__}>"
    if len(text) > _MAX_TEXT:
        text = text[: _MAX_TEXT - 3] + "..."
    return text


class _State:
    mode: tuple[str, str] | None = None
    depth = 0
    #: (function label, readable result or None, error text or None) per top-level call.
    recording: list[tuple[str, str | None, str | None]] | None = None


def _module_from_file(
    name: str, path: str, loader: importlib.machinery.SourceFileLoader | None = None
) -> ModuleType:
    """Import the Python file ``path`` as module ``name`` (registered in ``sys.modules``).

    Raises ImportError (instead of an AttributeError on ``None``) when Python cannot make an
    import spec for the path, e.g. because it does not end in ``.py``."""
    spec = importlib.util.spec_from_file_location(name, path, loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"Python cannot import {path} (it is not a Python source file)")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def probe(spec: dict[str, Any]) -> dict[str, Any]:  # noqa: C901 - one self-contained routine
    """Run the probe described by ``spec`` (see ``lawkeep.examples_table.run_probe``)."""
    import copy
    import functools
    import inspect
    import traceback

    state = _State()
    laws_path = Path(spec["laws_dir"])
    source_dirs = [Path(p) for p in spec["sys_path"]]

    from hypothesis import HealthCheck, Phase, settings
    from hypothesis.errors import UnsatisfiedAssumption

    control: tuple[type[BaseException], ...] = ()
    try:  # Hypothesis-internal control flow; must pass through untouched
        from hypothesis.errors import Frozen, StopTest

        control = (StopTest, Frozen)
    except ImportError:  # pragma: no cover
        pass
    data_object_types: tuple[type, ...] = ()
    try:  # Hypothesis-internal: the value given to a law for ``st.data()``
        from hypothesis.strategies._internal.core import DataObject

        data_object_types = (DataObject,)
    except ImportError:  # pragma: no cover
        pass
    # HealthCheck's metaclass hides deprecated members when iterated; the stubs type that
    # iteration as the metaclass, so say what the members are.
    all_health_checks = cast("list[HealthCheck]", list(HealthCheck))

    from lawkeep._law import get_law

    out: dict[str, Any] = {"versions": {}, "cases": [], "mutants": []}

    if spec.get("strategies_override"):
        try:
            _module_from_file("lawkeep_project_strategies", spec["strategies_override"])
        except Exception:  # noqa: BLE001
            sys.modules.pop("lawkeep_project_strategies", None)

    def is_project_file(path: str | None) -> bool:
        if not path:
            return False
        p = Path(path).resolve()
        parts = set(p.parts)
        if "site-packages" in parts or "dist-packages" in parts or ".venv" in parts:
            return False
        try:
            p.relative_to(laws_path)
            return False
        except ValueError:
            pass
        for d in source_dirs:
            try:
                p.relative_to(d)
                return True
            except ValueError:
                continue
        return False

    def source_of(obj: Any) -> str | None:
        try:
            return inspect.getsourcefile(obj)
        except (TypeError, OSError):
            return None

    targets: dict[str, str] = {}  # key -> label

    def mutate(
        fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any], variant: str
    ) -> object:
        from decimal import Decimal
        from fractions import Fraction

        if variant == "raises":
            raise RuntimeError("lawkeep: deliberately broken version")
        if variant == "none":
            return None
        if variant == "first_input":
            if args:
                return args[0]
            return next(iter(kwargs.values())) if kwargs else None
        returned: object = fn(*args, **kwargs)
        r = returned
        if isinstance(r, bool):
            return (not r) if variant == "flip" else r
        if isinstance(r, (int, float, Decimal, Fraction)):
            if variant == "zero":
                return r * 0
            if variant == "plus_one":
                return r + 1
            if variant == "minus_one":
                return r - 1
            if variant == "double":
                return r * 2 if r else r + 2
            return r
        if isinstance(r, (str, bytes, list, tuple, dict)):
            seq = cast("str | bytes | list[object] | tuple[object, ...] | dict[object, object]", r)
            if variant == "empty":
                return type(seq)()
            if variant == "drop_last" and not isinstance(seq, dict):
                return seq[:-1]
        return returned

    def wrap(fn: Callable[..., Any], label: str) -> Callable[..., Any]:
        if getattr(fn, "__lawkeep_probe__", False):
            return fn
        key = f"{getattr(fn, '__module__', '?')}:{getattr(fn, '__qualname__', label)}"
        targets.setdefault(key, label)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state.depth += 1
            try:
                top = state.depth == 1
                if top and state.mode is not None and state.mode[0] == key:
                    return mutate(fn, args, kwargs, state.mode[1])
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    if top and state.recording is not None:
                        state.recording.append((label, None, f"{type(exc).__name__}: {exc}"))
                    raise
                if top and state.recording is not None:
                    state.recording.append((label, readable(result), None))
                return result
            finally:
                state.depth -= 1

        setattr(wrapper, "__lawkeep_probe__", True)  # noqa: B010 - a marker, not a declared attribute
        return wrapper

    def install_wrappers(mod: ModuleType) -> None:
        for name, val in list(vars(mod).items()):
            if name.startswith("__"):
                continue
            if inspect.isfunction(val) and is_project_file(source_of(val)):
                setattr(mod, name, wrap(val, getattr(val, "__name__", name)))
            elif inspect.ismodule(val) and is_project_file(getattr(val, "__file__", None)):
                short = val.__name__.rsplit(".", 1)[-1]
                for n2, v2 in list(vars(val).items()):
                    if (
                        inspect.isfunction(v2)
                        and getattr(v2, "__module__", None) == val.__name__
                        and not n2.startswith("_")
                    ):
                        setattr(val, n2, wrap(v2, f"{short}.{n2}"))

    class Version:
        def __init__(self, cfg: dict[str, str], index: int):
            self.name = cfg["name"]
            self.file = cfg["file"]
            self.qualname = cfg["qualname"]
            self.index = index
            self.status = "ok"
            self.message: str | None = None
            self.kind = "unknown"
            self.obj: Any = None
            self.call: Any = None
            self.sig: inspect.Signature | None = None
            self.notes: list[str] = []
            self.sentence: str | None = None
            self.draw_error: str | None = None
            self.whole_run: dict[str, Any] | None = None
            self.explicit = 0

        def load(self) -> None:
            modname = f"lawkeep_probe_{self.index}_{self.name}"
            try:
                loader = importlib.machinery.SourceFileLoader(modname, self.file)
                mod = _module_from_file(modname, self.file, loader=loader)
            except BaseException as exc:  # noqa: BLE001 - report any import problem
                if isinstance(exc, KeyboardInterrupt):
                    raise
                tb = traceback.format_exception_only(type(exc), exc)
                self.status = "import_error"
                self.message = "The law file does not import: " + "".join(tb).strip()
                return
            obj: Any = mod
            for part in self.qualname.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    self.status = "import_error"
                    self.message = f"The law file has no {self.qualname!r}."
                    return
            self.obj = obj
            meta = get_law(obj)
            self.sentence = meta.sentence if meta else None
            install_wrappers(mod)
            if isinstance(obj, type):
                from hypothesis.stateful import RuleBasedStateMachine

                if issubclass(obj, RuleBasedStateMachine):
                    self.kind = "state_machine"
                else:
                    self.kind = "class"
                    self.status = "unsupported"
                    self.message = "This law is a test class; lawkeep cannot show examples for it."
                return
            if hasattr(obj, "hypothesis") and hasattr(obj.hypothesis, "inner_test"):
                self.kind = "hypothesis"
                self.call = obj.hypothesis.inner_test
                self.explicit = len(getattr(obj, "hypothesis_explicit_examples", ()) or ())
                outer = inspect.signature(obj)
            elif callable(obj):
                self.kind = "plain"
                self.call = obj
                outer = inspect.signature(obj)
            else:
                self.status = "unsupported"
                self.message = "lawkeep does not know how to run this law."
                return
            needed = [
                p.name
                for p in outer.parameters.values()
                if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            ]
            if needed:
                self.status = "unsupported"
                self.message = (
                    f"This law uses pytest fixtures ({', '.join(needed)}), so lawkeep can only "
                    f"run it inside pytest; examples cannot be shown."
                )
                return
            try:
                self.sig = inspect.signature(self.call)
            except (TypeError, ValueError):
                self.sig = None

        def info(self) -> dict[str, Any]:
            return {
                "status": self.status,
                "message": self.message,
                "kind": self.kind,
                "sentence": self.sentence,
                "notes": self.notes,
                "draw_error": self.draw_error,
                "whole_run": self.whole_run,
            }

    versions = [Version(v, i) for i, v in enumerate(spec["versions"])]
    for v in versions:
        v.load()

    mutant_list: list[dict[str, str]] = []
    if spec.get("mutants"):
        for key, label in sorted(targets.items()):
            for variant, text in _VARIANTS:
                mutant_list.append(
                    {
                        "id": f"{key}#{variant}",
                        "key": key,
                        "variant": variant,
                        "description": f"{label}() {text}",
                    }
                )
    out["mutants"] = [{"id": m["id"], "description": m["description"]} for m in mutant_list]

    def has_data_object(kwargs: dict[str, Any]) -> bool:
        return bool(data_object_types) and any(
            isinstance(x, data_object_types) for x in kwargs.values()
        )

    def evaluate(
        version: Version, kwargs: dict[str, Any], drawer: Version | None
    ) -> dict[str, Any]:
        if version.status != "ok" or version.call is None:
            return {"outcome": "incomparable"}
        if version is not drawer and has_data_object(kwargs):
            return {"outcome": "incomparable"}
        if version.sig is not None:
            try:
                version.sig.bind(**kwargs)
            except TypeError:
                return {"outcome": "incomparable"}
        try:
            args = copy.deepcopy(kwargs)
        except Exception:  # noqa: BLE001
            args = dict(kwargs)
        state.recording = []
        outcome: dict[str, Any]
        try:
            version.call(**args)
            outcome = {"outcome": "holds"}
        except UnsatisfiedAssumption:
            outcome = {"outcome": "skipped"}
        except control:
            raise
        except AssertionError as exc:
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else "an assertion failed"
            outcome = {"outcome": "fails", "message": msg[:300]}
        except BaseException as exc:  # noqa: BLE001 - pytest.skip() raises a BaseException
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if type(exc).__name__ in ("Skipped", "XFailed", "SkipTest"):
                outcome = {"outcome": "skipped", "message": "skipped"}
            else:
                outcome = {
                    "outcome": "fails",
                    "message": f"raised {type(exc).__name__}: {exc}"[:300],
                }
        finally:
            calls, state.recording = state.recording or [], None
        if calls:
            parts: list[str] = []
            seen: set[str] = set()
            for label, result, error in calls:
                text = f"{label}() → {result}" if error is None else f"{label}() raised {error}"
                if text in seen:
                    continue
                seen.add(text)
                parts.append(text)
                if len(parts) == 2:
                    break
            outcome["result"] = "; ".join(parts)[:200]
        return outcome

    seen_inputs: set[str] = set()

    def record_case(kwargs: dict[str, Any], drawer: Version | None, source: str) -> None:
        inputs = {k: readable(v) for k, v in kwargs.items()}
        key = json.dumps(inputs, sort_keys=True)
        if key in seen_inputs:
            return
        seen_inputs.add(key)
        case: dict[str, Any] = {"inputs": inputs, "source": source, "real": {}, "mutants": {}}
        for v in versions:
            case["real"][v.name] = evaluate(v, kwargs, drawer)
        if mutant_list and len(out["cases"]) < _MUTANT_CASES and not has_data_object(kwargs):
            for m in mutant_list:
                state.mode = (m["key"], m["variant"])
                try:
                    case["mutants"][m["id"]] = {
                        v.name: evaluate(v, kwargs, drawer)["outcome"] for v in versions
                    }
                finally:
                    state.mode = None
        out["cases"].append(case)

    run_settings = settings(
        max_examples=spec["examples"],
        database=None,
        derandomize=True,
        deadline=None,
        phases=[Phase.explicit, Phase.generate],
        suppress_health_check=all_health_checks,
        print_blob=False,
    )

    plain_done = False
    for v in versions:
        if v.status != "ok":
            continue
        if v.kind == "hypothesis":
            calls = {"n": 0}

            def recorder(_v: Version = v, _calls: dict[str, int] = calls, **kwargs: Any) -> None:
                _calls["n"] += 1
                source = "explicit" if _calls["n"] <= _v.explicit else "generated"
                record_case(kwargs, _v, source)

            original = v.obj.hypothesis.inner_test
            had = "_hypothesis_internal_use_settings" in vars(v.obj)
            old_settings = getattr(v.obj, "_hypothesis_internal_use_settings", None)
            old_seed = getattr(v.obj, "_hypothesis_internal_use_seed", None)
            v.obj.hypothesis.inner_test = recorder
            v.obj._hypothesis_internal_use_settings = run_settings
            # An explicit seed (what Hypothesis' @seed sets). derandomize alone is not enough
            # here: it hashes the test function, which is now `recorder`, and its signature's
            # repr holds object addresses that change from run to run.
            v.obj._hypothesis_internal_use_seed = spec.get("seed", 0)
            try:
                v.obj()
            except control:
                raise
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                v.draw_error = f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"[
                    :300
                ]
            finally:
                v.obj.hypothesis.inner_test = original
                v.obj._hypothesis_internal_use_seed = old_seed
                if had:
                    v.obj._hypothesis_internal_use_settings = old_settings
        elif v.kind == "plain" and not plain_done:
            plain_done = True
            record_case({}, v, "fixed")
        elif v.kind == "state_machine":
            from hypothesis.stateful import run_state_machine_as_test

            machine_seed = getattr(v.obj, "_hypothesis_internal_use_seed", None)
            v.obj._hypothesis_internal_use_seed = spec.get("seed", 0)  # as the gate does
            try:
                run_state_machine_as_test(
                    v.obj,
                    settings=settings(
                        max_examples=min(spec["examples"], 50),
                        database=None,
                        derandomize=True,
                        deadline=None,
                        suppress_health_check=all_health_checks,
                        print_blob=False,
                    ),
                )
                v.whole_run = {"outcome": "holds", "message": None}
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                first = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                v.whole_run = {
                    "outcome": "fails",
                    "message": f"{type(exc).__name__}: {first}"[:300],
                }
            finally:
                v.obj._hypothesis_internal_use_seed = machine_seed

    extras: list[dict[str, Any]] = spec.get("extra_inputs") or []
    for extra in extras:
        values = _literal_inputs(extra)
        if values is not None:
            record_case(values, None, "saved")

    out["versions"] = {v.name: v.info() for v in versions}
    return out


def _literal_inputs(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Turn ``{"amount": "Decimal('0.50')", "n": "3"}`` into values, or None if not literal."""
    import ast
    from decimal import Decimal, InvalidOperation

    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            out[k] = v
            continue
        m = re.fullmatch(r"Decimal\((['\"])([^'\"]*)\1\)", v.strip())
        try:
            out[k] = Decimal(m.group(2)) if m else ast.literal_eval(v)
        except (ValueError, SyntaxError, InvalidOperation, MemoryError, RecursionError):
            return None
    return out


def run(request: dict[str, Any], channel: ResultsChannel) -> int:
    """The ``probe`` mode's handler in the law-running child (see ``lawkeep._lawrun_child``)."""
    spec: dict[str, Any] = {**request, **(request.get("probe") or {})}
    try:
        result = probe(spec)
    except BaseException as exc:  # noqa: BLE001 - always send something readable
        import traceback

        result = {
            "error": "The example run crashed: "
            + "".join(traceback.format_exception_only(type(exc), exc)).strip()
        }
    channel.send({"type": "probe", "result": result})
    channel.send({"type": "done"})
    return 0
