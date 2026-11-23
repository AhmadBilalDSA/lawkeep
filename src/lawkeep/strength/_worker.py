"""The ``strength`` mode of the law-running core: runs laws and measures them.

Runs inside the isolated child started by ``lawkeep.lawrun.run(..., "strength", ...)``
(``lawkeep._lawrun_child`` calls :func:`run`), so it has the same isolation rules as the gate:
``python -I -S -B`` through ``_boot.py``, cwd = project root, plugin autoload off, only the
requested law files, ``--noconftest`` (only the fixtures of ``laws/conftest.py`` are loaded,
through the gate plugin's ``load_laws_fixtures``), no cache, no user ``pytest.ini``,
``--import-mode=importlib``, ``PYTHONHASHSEED=0``, private Hypothesis storage.

Two kinds of request (``strength.kind``):

- ``map``: run each law once (its baseline) under coverage.py with one coverage *context* per
  law, collecting Hypothesis run statistics. Emits which lines each law executed.
- ``probe``: for each law, temporarily swap functions for dumb versions (quick check) and plant
  bugs (see :mod:`lawkeep.strength.mutants`), re-running the law each time. Swaps happen only in
  this process's memory; no file is written. Every swap is undone right after the run.

Results are sent on the results channel as records (one per event, as they happen, so partial
results survive a timeout), ending with a ``done`` record.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import os
import signal
import sys
import textwrap
import threading
import time
import traceback
import types
import typing
import unittest
from collections import Counter
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest

if TYPE_CHECKING:
    import coverage
    from hypothesis.utils.dynamicvariables import DynamicVariable

    from lawkeep._law import LawMeta
    from lawkeep.lawrun import ResultsChannel

#: Writes one result record to the results file.
Emit = Callable[[dict[str, Any]], None]
#: The body of a dumb version: (positional args, keyword args) -> return value.
DumbBody = Callable[[tuple[Any, ...], dict[str, Any]], Any]

_PROJECT_MODULE_SKIP = ("site-packages", "dist-packages", "/.venv/", "/venv/")


class _ProbeTimeout(BaseException):
    """Raised by the alarm when one law run takes too long (an endless loop, usually)."""


@contextlib.contextmanager
def _alarm(seconds: float | None) -> Generator[None, None, None]:
    if (
        not seconds
        or seconds <= 0
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def handler(signum: int, frame: types.FrameType | None) -> NoReturn:  # pragma: no cover
        raise _ProbeTimeout()

    old = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _first_line(text: str, limit: int = 300) -> str:
    for line in str(text).strip().splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _is_assertion(exc: object) -> bool:
    if isinstance(exc, AssertionError):
        return True
    group: object = getattr(exc, "exceptions", None)
    if isinstance(group, (list, tuple)) and group:
        return all(_is_assertion(e) for e in cast("Sequence[object]", group))
    return False


class _Target:
    """A function (or method) in the project that can be swapped temporarily."""

    def __init__(self, module: types.ModuleType, owner: Any, name: str, raw: Any) -> None:
        self.module = module
        self.owner = owner
        self.name = name
        self.raw = raw  # what is stored on the owner (function, staticmethod, property, ...)
        self.owner_is_class = isinstance(owner, type)

    @property
    def func(self) -> Any:
        raw = self.raw
        if isinstance(raw, staticmethod):
            return cast("staticmethod[..., Any]", raw).__func__
        if isinstance(raw, classmethod):
            return cast("classmethod[Any, ..., Any]", raw).__func__
        if isinstance(raw, property):
            return raw.fget
        return raw

    @property
    def skip_first(self) -> bool:
        if not self.owner_is_class:
            return False
        return not isinstance(self.raw, staticmethod)


class _Swap:
    """Replace a target everywhere it is bound in project modules; undo on exit."""

    def __init__(self, target: _Target, new_raw: Any, modules: list[types.ModuleType]) -> None:
        self.target = target
        self.new_raw = new_raw
        self.modules = modules
        self.undo: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> _Swap:
        t = self.target
        if t.owner_is_class:
            self.undo.append((t.owner, t.name, t.raw))
            setattr(t.owner, t.name, self.new_raw)
            return self
        orig = t.raw
        done: set[tuple[int, str]] = set()
        for mod in [t.module, *self.modules]:
            d: object = getattr(mod, "__dict__", None)
            if not isinstance(d, dict):
                continue
            for attr, val in list(cast("dict[str, object]", d).items()):
                if val is orig and (id(mod), attr) not in done:
                    done.add((id(mod), attr))
                    self.undo.append((mod, attr, val))
                    setattr(mod, attr, self.new_raw)
        return self

    def __exit__(self, *exc: Any) -> None:
        for owner, attr, val in reversed(self.undo):
            setattr(owner, attr, val)
        self.undo.clear()


# ---------------------------------------------------------------------------------------------
# Dumb versions (quick check)


def _returns_value(func: Any) -> bool:
    """False for procedures (no `return <value>`, no yield): dumb return values say nothing."""
    try:
        src = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        return True
    top = tree.body[0] if tree.body else None
    if top is None:
        return True
    for node in ast.walk(top):
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                return True
    return False


def _empty_value(func: Any) -> tuple[bool, Any]:
    """A zero-argument default of the annotated return type (``""``, ``[]``, ``Money()``)."""
    try:
        hint = typing.get_type_hints(func).get("return")
    except Exception:
        return False, None
    origin = typing.get_origin(hint) or hint
    if not isinstance(origin, type) or origin in (int, bool, float, type(None), object):
        return False, None
    try:
        value = origin()
    except Exception:
        return False, None
    return True, value


def _positional_params(func: Any, skip_first: bool) -> list[str]:
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return []
    names = [
        p.name
        for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    return names[1:] if skip_first and names else names


def quick_variants(target: _Target) -> list[tuple[str, str, Any]]:
    """(variant, plain description, new raw object) for one target."""
    func = target.func
    if not callable(func):
        return []
    name = getattr(func, "__name__", target.name)
    skip = 1 if target.skip_first else 0
    is_async = inspect.iscoroutinefunction(func)
    out: list[tuple[str, str, Any]] = []

    def build(body: DumbBody) -> object:
        dumb: Callable[..., Any]
        if is_async:

            async def dumb_async(*args: Any, **kwargs: Any) -> Any:
                return body(args, kwargs)

            dumb = dumb_async
        else:

            def dumb_sync(*args: Any, **kwargs: Any) -> Any:
                return body(args, kwargs)

            dumb = dumb_sync

        dumb.__name__ = name
        dumb.__qualname__ = getattr(func, "__qualname__", name)
        dumb.__module__ = cast(str, getattr(func, "__module__", None))  # may be None, as before
        raw = target.raw
        if isinstance(raw, staticmethod):
            return staticmethod(dumb)
        if isinstance(raw, classmethod):
            return classmethod(dumb)
        if isinstance(raw, property):
            return property(dumb, raw.fset, raw.fdel, raw.__doc__)
        return dumb

    if _returns_value(func):
        out.append(("always_none", f"{name}() always returns None", build(lambda a, k: None)))
        out.append(("always_zero", f"{name}() always returns 0", build(lambda a, k: 0)))
        ok, empty = _empty_value(func)
        if ok:
            out.append(
                (
                    "always_empty",
                    f"{name}() always returns {empty!r}",
                    build(lambda a, k, empty=empty: empty),
                )
            )
        params = _positional_params(func, target.skip_first)
        if params:

            def first(
                a: tuple[Any, ...], k: dict[str, Any], skip: int = skip, p0: str = params[0]
            ) -> Any:
                if len(a) > skip:
                    return a[skip]
                if p0 in k:
                    return k[p0]
                return next(iter(k.values()), None)

            out.append(
                (
                    "returns_first_argument",
                    f"{name}() always returns its first argument ({params[0]}) unchanged",
                    build(first),
                )
            )

    def boom(a: tuple[Any, ...], k: dict[str, Any], name: str = name) -> NoReturn:
        raise RuntimeError(f"lawkeep quick check: a dumb version of {name}() that always fails")

    out.append(("raises", f"{name}() always raises an error", build(boom)))
    return out


# ---------------------------------------------------------------------------------------------
# The pytest plugin


class StrengthPlugin:
    def __init__(self, req: dict[str, Any], emit: Emit) -> None:
        self.req = req
        self.emit = emit
        self.root = Path(req["root"]).resolve()
        self.mode = req["kind"]
        self.law_ids = set(req.get("law_ids") or []) or None
        self.keys: dict[str, str] = {}  # nodeid -> law key
        self.probes: dict[str, Any] = req.get("probes") or {}
        self.cov: coverage.Coverage | None = None
        self._metas: dict[str, tuple[LawMeta, str]] = {}
        self._mutant_cache: dict[tuple[str, str, int], Any] = {}
        self._file_cache: dict[int, tuple[Any, str | None]] = {}  # id(module) -> (module, path)

    # -- helpers ---------------------------------------------------------------------------
    def rel(self, path: Any) -> str:
        p = Path(str(path)).resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return p.as_posix()

    @staticmethod
    def law_meta(item: Any) -> LawMeta | None:
        from lawkeep._law import get_law

        for obj in (
            getattr(item, "obj", None),
            getattr(getattr(item, "parent", None), "obj", None),
        ):
            if obj is None:
                continue
            try:
                meta = get_law(obj)
            except Exception:
                meta = None
            if meta is not None:
                return meta
        return None

    def law_source_names(self, item: Any, meta: LawMeta) -> list[str]:
        obj: Any = None
        module = getattr(item, "module", None)
        if module is not None and meta.qualname:
            obj = module
            for part in meta.qualname.split("."):
                obj = getattr(obj, part, None)
                if obj is None:
                    break
        if obj is None:
            obj = getattr(item, "obj", None)
        inner = getattr(getattr(obj, "hypothesis", None), "inner_test", None) or obj
        try:
            src = textwrap.dedent(inspect.getsource(inner))
            tree = ast.parse(src)
        except (OSError, TypeError, SyntaxError):
            return []
        names: set[str] = set()
        bound: set[str] = set()  # the law's own inputs and local variables, not functions
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Store):
                    bound.add(node.id)
                else:
                    names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        return sorted((names - bound) | called)

    def _file_of(self, mod: Any) -> str | None:
        """The resolved file of a module (cached; None for built-ins and namespaces)."""
        cached = self._file_cache.get(id(mod))
        if cached is not None and cached[0] is mod:
            return cached[1]
        f = getattr(mod, "__file__", None)
        path: str | None = None
        if isinstance(f, str):
            try:
                path = str(Path(f).resolve())
            except OSError:
                path = None
        self._file_cache[id(mod)] = (mod, path)
        return path

    def project_modules(self, extra: Any = None) -> list[types.ModuleType]:
        """Loaded modules from the project folder (not installed packages): the places where
        a swapped function may be bound by name (``from shop.refunds import refund``)."""
        root = str(self.root).rstrip(os.sep) + os.sep
        out: list[types.ModuleType] = []
        for mod in list(sys.modules.values()):
            fp = self._file_of(mod)
            if not fp or not fp.startswith(root):
                continue
            if any(s in fp.replace(os.sep, "/") for s in _PROJECT_MODULE_SKIP):
                continue
            out.append(mod)
        if extra is not None and extra not in out:
            out.append(extra)
        return out

    def module_for(self, file: str, module_name: str | None) -> types.ModuleType | None:
        abs_file = str((self.root / file).resolve())
        for mod in list(sys.modules.values()):
            if self._file_of(mod) == abs_file:
                return mod
        if module_name:
            try:
                mod = importlib.import_module(module_name)
            except Exception:
                return None
            if self._file_of(mod) == abs_file:
                return mod
        return None

    def target(self, file: str, qualname: str, module_name: str | None) -> _Target | None:
        mod = self.module_for(file, module_name)
        if mod is None:
            return None
        owner: Any = mod
        parts = qualname.split(".")
        for p in parts[:-1]:
            owner = getattr(owner, p, None)
            if owner is None:
                return None
        name = parts[-1]
        if isinstance(owner, type):
            raw = owner.__dict__.get(name)
        else:
            raw = getattr(owner, name, None)
        if raw is None:
            return None
        return _Target(mod, owner, name, raw)

    def mutant_raw(self, target: _Target, file: str, qualname: str, index: int) -> Any:
        key = (file, qualname, index)
        if key in self._mutant_cache:
            return self._mutant_cache[key]
        from lawkeep.strength.mutants import mutated_module

        abs_file = str((self.root / file).resolve())
        source = Path(abs_file).read_text(encoding="utf-8")
        module_ast = mutated_module(source, qualname, index)
        code = compile(module_ast, abs_file, "exec")
        ns: dict[str, Any] = {}
        globs = target.module.__dict__
        if target.owner_is_class:
            # class-level names used in decorators are visible through the class namespace
            owner_ns = cast("dict[str, Any]", vars(target.owner))
            ns.update({k: v for k, v in owner_ns.items() if not k.startswith("__")})
            before = dict(ns)
            exec(code, globs, ns)
            new = ns.get(target.name)
            if new is before.get(target.name):
                raise LookupError("planted bug did not define the method")
        else:
            exec(code, globs, ns)
            new = ns[target.name]
        self._mutant_cache[key] = new
        return new

    # -- running one law -------------------------------------------------------------------
    def call_law(self, item: pytest.Item) -> None:
        from _pytest.unittest import TestCaseFunction

        cls = getattr(getattr(item, "parent", None), "obj", None)
        # TestCaseFunction (a pytest.Function subclass) records failures instead of raising
        # them, so unittest-style laws (state machines) are run through unittest directly.
        if (
            isinstance(item, TestCaseFunction)
            and isinstance(cls, type)
            and issubclass(cls, unittest.TestCase)
        ):
            result = unittest.TestResult()
            cls(item.name).run(result)
            problems = result.failures + result.errors
            if problems:
                text = problems[0][1]
                if result.failures:
                    raise AssertionError(_first_line(text.splitlines()[-1] if text else ""))
                raise RuntimeError(_first_line(text.splitlines()[-1] if text else ""))
            if result.skipped:
                pytest.skip(result.skipped[0][1])
            return
        # the class's runtest: the instance attribute is replaced in pytest_runtest_call
        runtest = cast("Callable[[pytest.Item], None]", type(item).runtest)
        runtest(item)

    def run_law(self, item: pytest.Item, timeout: float | None) -> tuple[str, str | None, float]:
        t0 = time.perf_counter()
        try:
            with _alarm(timeout):
                self.call_law(item)
            return "holds", None, time.perf_counter() - t0
        except _ProbeTimeout:
            return (
                "timeout",
                f"stopped after {timeout:.0f}s (ran too long)",
                time.perf_counter() - t0,
            )
        except KeyboardInterrupt:
            raise
        except pytest.skip.Exception as exc:
            return "skipped", _first_line(str(exc)), time.perf_counter() - t0
        except BaseException as exc:
            status = "fails" if _is_assertion(exc) else "error"
            msg = _first_line(f"{type(exc).__name__}: {exc}")
            return status, msg, time.perf_counter() - t0

    # -- pytest hooks ----------------------------------------------------------------------
    def pytest_configure(self, config: Any) -> None:
        from lawkeep.pytest_plugin import load_laws_fixtures

        config.addinivalue_line("markers", "law(sentence, law_id=None): a lawkeep law")
        errors, _notes = load_laws_fixtures(self.root, config.pluginmanager)
        for err in errors:
            self.emit({"type": "collection_error", "file": err["path"], "message": err["message"]})

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            text = str(report.longrepr) if report.longrepr else "could not be collected"
            last = [ln for ln in text.strip().splitlines() if ln.strip()]
            self.emit(
                {
                    "type": "collection_error",
                    "file": self.rel(report.fspath) if report.fspath else None,
                    "message": (last[-1] if last else text)[:500],
                }
            )

    @pytest.hookimpl(tryfirst=True)
    def pytest_pycollect_makeitem(self, collector: Any, name: str, obj: Any) -> object:
        from lawkeep._law import get_law, is_state_machine

        try:
            meta = get_law(obj)
        except Exception:
            return None
        if meta is None:
            return None
        if isinstance(obj, type):
            machine = cast("type[Any]", obj)
            tc: object = machine.TestCase if is_state_machine(machine) else machine
            if isinstance(tc, type) and issubclass(tc, unittest.TestCase):
                if tc is obj and collector.classnamefilter(name):
                    return None
                from _pytest.unittest import UnitTestCase

                node = UnitTestCase.from_parent(  # pyright: ignore[reportUnknownMemberType]  # pytest leaves `parent` and `**kw` unannotated
                    collector, name=name
                )
                # a state machine's TestCase, not the machine class itself; pytest has no
                # public way to give a UnitTestCase node a different class
                node._obj = tc  # pyright: ignore[reportPrivateUsage]  # see the comment above
                return node
            return None
        if callable(obj) and not collector.funcnamefilter(name):
            return list(collector._genfunctions(name, obj))
        return None

    def pytest_collection_modifyitems(
        self, session: Any, config: Any, items: list[pytest.Item]
    ) -> None:
        keep: list[pytest.Item] = []
        seen: set[tuple[str, str]] = set()
        metas: dict[str, tuple[LawMeta, str]] = {}
        for item in items:
            meta = self.law_meta(item)
            if meta is None:
                continue
            if self.law_ids and meta.law_id not in self.law_ids:
                continue
            relfile = self.rel(item.path)
            if (meta.law_id, relfile) in seen:
                continue
            seen.add((meta.law_id, relfile))
            metas[item.nodeid] = (meta, relfile)
            keep.append(item)
        counts = Counter(meta.law_id for meta, _ in metas.values())
        for nodeid, (meta, relfile) in metas.items():
            self.keys[nodeid] = (
                meta.law_id if counts[meta.law_id] == 1 else f"{meta.law_id}@{relfile}"
            )
        if self.mode == "probe":
            keep = [i for i in keep if self.keys[i.nodeid] in self.probes]
        items[:] = keep
        self._metas = metas

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        key = self.keys.get(item.nodeid)
        try:
            if key is not None:
                meta, relfile = self._metas[item.nodeid]
                if self.mode == "map":
                    self.map_one(item, key, meta, relfile)
                else:
                    self.probe_one(item, key)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # never let lawkeep's own problem look like a law result
            self.emit(
                {
                    "type": "worker_error",
                    "key": key,
                    "message": _first_line(f"{type(exc).__name__}: {exc}"),
                    "traceback": traceback.format_exc()[-2000:],
                }
            )
        item.runtest = lambda: None  # the work is done; pytest's own call becomes a no-op
        return (yield)

    # -- map mode --------------------------------------------------------------------------
    def map_one(self, item: pytest.Item, key: str, meta: LawMeta, relfile: str) -> None:
        from hypothesis.statistics import collector as _collector

        # Hypothesis declares the collector as DynamicVariable(None); it holds a callback.
        collector = cast("DynamicVariable[Emit | None]", _collector)

        stats: list[dict[str, Any]] = []
        if self.cov is not None:
            self.cov.switch_context(key)
        try:
            with collector.with_value(stats.append):
                status, message, duration = self.run_law(item, self.req.get("timeout"))
        finally:
            if self.cov is not None:
                self.cov.switch_context("")
        self.emit(
            {
                "type": "law",
                "key": key,
                "law_id": meta.law_id,
                "sentence": meta.sentence,
                "file": relfile,
                "kind": meta.kind,
                "status": status,
                "message": message,
                "duration": round(duration, 4),
                "names": self.law_source_names(item, meta),
                "stats": _summarise_stats(stats),
            }
        )

    # -- probe mode ------------------------------------------------------------------------
    def probe_one(self, item: pytest.Item, key: str) -> None:
        plan: dict[str, Any] = self.probes.get(key) or {}
        timeout = plan.get("timeout") or self.req.get("timeout")
        modules = self.project_modules(getattr(item, "module", None))
        for file, qualname, module_name in plan.get("quick", []):
            target = self.target(file, qualname, module_name)
            if target is None:
                self.emit(
                    {
                        "type": "quick",
                        "key": key,
                        "file": file,
                        "qualname": qualname,
                        "variant": None,
                        "status": "not_found",
                        "message": "could not find this function while the law ran",
                    }
                )
                continue
            for variant, desc, raw in quick_variants(target):
                with _Swap(target, raw, modules):
                    status, message, duration = self.run_law(item, timeout)
                self.emit(
                    {
                        "type": "quick",
                        "key": key,
                        "file": file,
                        "qualname": qualname,
                        "variant": variant,
                        "description": desc,
                        "status": status,
                        "message": message,
                        "duration": round(duration, 4),
                    }
                )
        for file, qualname, module_name, index in plan.get("mutants", []):
            record = {
                "type": "mutant",
                "key": key,
                "file": file,
                "qualname": qualname,
                "index": index,
            }
            target = self.target(file, qualname, module_name)
            if target is None:
                self.emit(
                    {
                        **record,
                        "status": "not_planted",
                        "message": "could not find this function while the law ran",
                    }
                )
                continue
            try:
                raw = self.mutant_raw(target, file, qualname, index)
            except Exception as exc:
                self.emit(
                    {
                        **record,
                        "status": "not_planted",
                        "message": _first_line(f"{type(exc).__name__}: {exc}"),
                    }
                )
                continue
            with _Swap(target, raw, modules):
                status, message, duration = self.run_law(item, timeout)
            self.emit(
                {**record, "status": status, "message": message, "duration": round(duration, 4)}
            )


def _summarise_stats(stats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Turn Hypothesis's run statistics into a few numbers (None for non-Hypothesis laws)."""
    if not stats:
        return None
    s = stats[-1]
    gen: dict[str, Any] = s.get("generate-phase") or {}
    cases: list[dict[str, Any]] = gen.get("test-cases") or []
    statuses = Counter[Any](c.get("status") for c in cases)
    events = Counter[str]()
    for c in cases:
        case_events: list[object] = c.get("events") or []
        for e in case_events:
            events[str(e)] += 1
    return {
        "generated": len(cases),
        "valid": statuses.get("valid", 0),
        "invalid": statuses.get("invalid", 0) + statuses.get("overrun", 0),
        "failing": statuses.get("interesting", 0),
        "stopped_because": s.get("stopped-because"),
        "events": dict(events.most_common(8)),
    }


def _load_profile(req: dict[str, Any]) -> None:
    from hypothesis import HealthCheck, Phase, Verbosity, settings

    settings.register_profile(
        "lawkeep-strength",
        max_examples=max(1, int(req.get("examples") or 50)),
        derandomize=True,
        database=None,
        deadline=None,
        suppress_health_check=list(HealthCheck),
        phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target],
        verbosity=Verbosity.quiet,
        print_blob=False,
    )
    settings.load_profile("lawkeep-strength")


def _start_coverage(req: dict[str, Any]) -> coverage.Coverage:
    import coverage

    root = str(Path(req["root"]).resolve())
    omit = [
        os.path.join(root, "laws", "*"),
        os.path.join(root, ".venv", "*"),
        "*/site-packages/*",
        "*/dist-packages/*",
    ]
    cov = coverage.Coverage(
        data_file=None, source=req.get("source_dirs") or [root], omit=omit, config_file=False
    )
    cov.set_option(
        "run:disable_warnings",
        ["no-data-collected", "module-not-imported", "module-never-imported", "couldnt-parse"],
    )
    cov.start()
    return cov


def _emit_coverage(cov: coverage.Coverage, plugin: StrengthPlugin) -> None:
    data = cov.get_data()
    by_key: dict[str, dict[str, list[int]]] = {}
    statements: dict[str, list[int]] = {}
    for f in data.measured_files():
        relf = plugin.rel(f)
        try:
            ctx_lines = data.contexts_by_lineno(f)
        except Exception:
            continue
        touched = False
        for line, ctxs in ctx_lines.items():
            for ctx in ctxs:
                if ctx:
                    by_key.setdefault(ctx, {}).setdefault(relf, []).append(int(line))
                    touched = True
        if touched:
            try:
                statements[relf] = sorted(int(n) for n in cov.analysis2(f)[1])
            except Exception:
                pass
    for key, files in by_key.items():
        plugin.emit(
            {"type": "coverage", "key": key, "lines": {k: sorted(set(v)) for k, v in files.items()}}
        )
    for relf, lines in statements.items():
        plugin.emit({"type": "statements", "file": relf, "lines": lines})


def run(request: dict[str, Any], channel: ResultsChannel) -> int:
    """The ``strength`` mode's handler in the law-running child (see ``lawkeep._lawrun_child``)."""
    req: dict[str, Any] = {**request, **(request.get("strength") or {})}

    def emit(rec: dict[str, Any]) -> None:
        channel.send(rec, default=repr)

    cov = None
    try:
        _load_profile(req)
        plugin = StrengthPlugin(req, emit)
        if req["kind"] == "map":
            try:
                cov = _start_coverage(req)
            except Exception as exc:
                emit({"type": "note", "message": f"coverage could not start: {exc}"})
            plugin.cov = cov
        code = pytest.main([str(a) for a in req["pytest_args"]], plugins=[plugin])
        if cov is not None:
            cov.stop()
            _emit_coverage(cov, plugin)
        emit({"type": "done", "pytest_exit": int(code)})
        return 0
    except BaseException as exc:  # pragma: no cover - reported to the parent
        emit(
            {
                "type": "fatal",
                "message": _first_line(f"{type(exc).__name__}: {exc}"),
                "traceback": traceback.format_exc()[-3000:],
            }
        )
        return 3
