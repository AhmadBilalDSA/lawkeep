"""lawkeep's pytest plugin: the ``check`` mode of the law-running core.

Owner: workstream B (gate).

Loaded two ways:

- by the gate: ``lawkeep.lawrun.run(..., "check", ...)`` starts the isolated child
  (``lawkeep._lawrun_child``), which calls :func:`run_check`; that switches the plugin on and
  runs pytest with ``-p _hypothesis_pytestplugin -p lawkeep.pytest_plugin`` (plugin autoload
  disabled, see ``lawkeep.runner``);
- automatically through the ``pytest11`` entry point in any project where lawkeep is installed.
  Then it stays PASSIVE: it only registers the ``law`` marker, so normal test suites are
  unaffected. Nothing in the environment can switch it on: only :func:`run_check` does.

The gate's settings come from the request the parent sent on stdin (never from environment
variables): ``root``, ``gate.profile`` (``config.Profile`` as a dict: examples, derandomize,
backend, deadline_ms, seed), ``gate.law_ids`` (empty = all) and ``gate.ledger`` (path of the
saved failing examples file to replay; missing = ``laws/failing_examples.jsonl`` under the
root; "" = none). The child already put the project's source folders on ``sys.path`` and
removed every ``LAWKEEP_*`` variable except ``LAWKEEP_ROOT`` (``lawkeep.st`` needs it).

What it does when active:

- loads only the *fixtures* of ``laws/conftest.py`` (hooks in that file are ignored, and no
  other conftest.py is read: the runner passes ``--noconftest``; see
  :func:`load_laws_fixtures`, shared with the strength mode);
- collects every ``@law`` function or class in the given files whatever its name, including
  Hypothesis state machines; plain tests without ``@law`` are NOT run: they are listed in
  the results as ``ignored`` (the gate shows a note telling you to add ``@law("...")``);
- applies the check profile to each Hypothesis law: at least ``examples`` inputs (a law asking
  for more keeps its own number; asking for fewer has no effect), all phases on, the profile's
  deadline and backend, a fixed seed per law when ``derandomize`` is on, and a per-law example
  database that replays saved failing examples first;
- records per law: result, examples tried (Hypothesis statistics), the simplest failing
  example (Hypothesis's shrunk "Falsifying example", split into arguments), failure message,
  time, and newly found failing choice sequences;
- sends everything as ONE ``{"type": "check", ...}`` record on the results channel and closes
  it straight away (see :meth:`_Gate._write_results`).

xfail does not hide a failing law: a law that fails under ``xfail`` is reported as failing.
A law is reported as ``skipped`` only when a ``skip``/``skipif`` marker written on it in laws/
skipped it (the integrity check flags skip markers that were not recorded in the law history).
A skip raised while the law runs (``pytest.skip()``, ``unittest.SkipTest`` from the law or
from the code it calls) is an ``error``, not a pass, and so is ``assume()``/``reject()``
called from outside laws/ (for example by the code under test to hide inputs it gets wrong).
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import re
import sys
import time
import types
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, TypeVar, cast

import pytest

from lawkeep._law import MARKER_NAME, LawMeta

if TYPE_CHECKING:
    from hypothesis import settings as Settings
    from hypothesis.stateful import RuleBasedStateMachine
    from hypothesis.utils.dynamicvariables import DynamicVariable

    from lawkeep.lawrun import ResultsChannel

#: What pytest_pycollect_makeitem may return.
_Collected = pytest.Item | pytest.Collector | list[pytest.Item | pytest.Collector] | None

ENV_ROOT = "LAWKEEP_ROOT"


@dataclass
class _Activation:
    """The gate's settings for this process (set only by :func:`run_check`)."""

    root: Path
    profile: dict[str, Any]
    law_ids: list[str]
    ledger: str | None
    channel: ResultsChannel


_active: _Activation | None = None


def run_check(request: dict[str, Any], channel: ResultsChannel) -> int:
    """The ``check`` mode's handler in the law-running child (see ``lawkeep._lawrun_child``)."""
    global _active
    gate: dict[str, Any] = request.get("gate") or {}
    _active = _Activation(
        root=Path(request.get("root") or os.getcwd()).resolve(),
        profile=dict(gate.get("profile") or {}),
        law_ids=[str(i) for i in cast("list[object]", gate.get("law_ids") or [])],
        ledger=gate.get("ledger"),
        channel=channel,
    )
    try:
        return int(pytest.main([str(a) for a in request["pytest_args"]]))
    finally:
        _active = None


RESULTS_VERSION = 1
#: Attributes set on lawkeep's wrappers so they are installed only once per process.
_GUARD_ATTR = "_lawkeep_guard"
_WRAPPED_ATTR = "_lawkeep_wrapped"
_GATE_PLUGIN_NAME = "lawkeep-gate"
_LAWS_FIXTURES_NAME = "lawkeep-laws-fixtures"

#: Hypothesis errors that mean "the law could not run properly" rather than "the code is wrong".
_SETUP_ERROR_NAMES = frozenset(
    {
        "Unsatisfiable",
        "FailedHealthCheck",
        "InvalidArgument",
        "InvalidDefinition",
        "InvalidState",
        "HypothesisDeprecationWarning",
        "ResolutionFailed",
        "BackendCannotProceed",
    }
)


def pytest_configure(config: pytest.Config) -> None:  # pragma: no cover - exercised through pytest
    config.addinivalue_line(
        "markers",
        f"{MARKER_NAME}(sentence, law_id=None): this test is a lawkeep law with a plain-English sentence",
    )
    if _active is None:
        return  # passive: normal pytest runs are unaffected
    if config.pluginmanager.has_plugin(_GATE_PLUGIN_NAME):
        return
    gate = _Gate(config, _active)
    config.pluginmanager.register(gate, _GATE_PLUGIN_NAME)


# ---------------------------------------------------------------------------------------------
# Helpers that do not need pytest state (unit-tested directly)


def stable_seed(law_id: str) -> int:
    """A fixed seed per law, so the same code always gets the same verdict."""
    return int.from_bytes(hashlib.sha256(law_id.encode("utf-8")).digest()[:8], "big")


_FALSIFYING_RE = re.compile(
    r"^(?:Falsifying|Failing) (?:explicit )?(?:example|test case):\s*", re.S
)
_ARG_LINE_RE = re.compile(r"^    ([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_falsifying_example(note: str) -> dict[str, str] | None:
    """Turn Hypothesis's "Failing test case: law(\\n    x=1,\\n)" note into ``{"x": "1"}``.

    Older Hypothesis versions write "Falsifying example:"; both are understood. Hypothesis's
    trailing comments (", # or any other generated value") are dropped.

    Returns ``{"call": "<text>"}`` when the arguments cannot be split (for example a state
    machine's steps), or ``None`` when ``note`` is not a falsifying-example note.
    """
    m = _FALSIFYING_RE.match(note)
    if not m:
        return None
    body = note[m.end() :].strip()
    call = re.match(r"^([A-Za-z_][\w.]*)\((.*)\)$", body, re.S)
    if not call:
        return {"call": body} if body else None
    inner = call.group(2)
    if not inner.strip():
        return {}
    lines = inner.split("\n")
    if len(lines) == 1:
        # short form on one line: f(x=1) - only split when it is a single argument
        one = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*?),?(?:\s+#.*)?\s*$", inner)
        return {one.group(1): one.group(2)} if one else {"call": body}
    args: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if not line.strip():
            continue
        am = _ARG_LINE_RE.match(line)
        if am:
            name: str = am.group(1)
            current = name
            args[name] = [am.group(2)]
        elif current is not None:
            args[current].append(line[4:] if line.startswith("    ") else line)
        else:
            return {"call": body}
    out: dict[str, str] = {}
    for k, parts in args.items():
        value = "\n".join(parts).rstrip()
        if ",  # " in parts[-1]:
            value = value[: value.rfind(",  # ")]
        if value.endswith(","):
            value = value[:-1]
        out[k] = value
    return out


def _draw_notes(notes: list[str]) -> dict[str, str]:
    """``Draw 1: 5`` notes (from ``st.data()``) as extra example entries."""
    out: dict[str, str] = {}
    for note in notes:
        m = re.match(r"^Draw (\d+)(?: \(([^)]*)\))?: (.*)$", note, re.S)
        if m:
            label = f"draw {m.group(1)}" + (f" ({m.group(2)})" if m.group(2) else "")
            out[label] = m.group(3).strip()
    return out


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    subs: object = getattr(exc, "exceptions", None)
    if isinstance(subs, (list, tuple)) and subs:
        out: list[BaseException] = []
        for s in cast("list[object] | tuple[object, ...]", subs):
            if isinstance(s, BaseException):
                out.extend(_leaf_exceptions(s))
        return out
    return [exc]


def examples_from_exception(exc: BaseException) -> list[dict[str, str]]:
    """Every falsifying example attached (as notes) to ``exc`` or its sub-exceptions."""
    found: list[dict[str, str]] = []
    for leaf in [exc, *_leaf_exceptions(exc)]:
        raw_notes = cast("list[object]", getattr(leaf, "__notes__", []) or [])
        notes = [n for n in raw_notes if isinstance(n, str)]
        for n, note in enumerate(notes):
            ex = parse_falsifying_example(note)
            if ex == {"call": "<state machine>"}:
                # A state machine's steps follow as separate notes ("state = M()", ...).
                steps: list[str] = []
                for step in notes[n + 1 :]:
                    if parse_falsifying_example(step) is not None:
                        break
                    if step.startswith(("state", "v")):
                        steps.append(step)
                if steps:
                    ex = {"call": "\n".join(steps)}
            if ex is not None:
                ex.update(_draw_notes(notes))
                if ex not in found:
                    found.append(ex)
    return found


def failure_text(exc: BaseException, *, max_lines: int = 12) -> str:
    """Plain text of the failure: ``ExceptionType: message`` (Hypothesis notes left out)."""
    leaves = _leaf_exceptions(exc)
    parts: list[str] = []
    for leaf in leaves[:3]:
        msg = str(leaf).strip()
        name = type(leaf).__name__
        text = f"{name}: {msg}" if msg else name
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = [*lines[:max_lines], "..."]
        parts.append("\n".join(lines))
    if len(leaves) > 3:
        parts.append(f"... and {len(leaves) - 3} more distinct failure(s)")
    if len(leaves) > 1:
        parts.insert(0, f"{len(leaves)} distinct failures were found:")
    return "\n".join(parts)


def is_setup_error(exc: BaseException) -> bool:
    """True when the exception means the law could not run properly (not a code bug)."""
    for leaf in [exc, *_leaf_exceptions(exc)]:
        for cls in type(leaf).__mro__:
            if cls.__name__ in _SETUP_ERROR_NAMES and cls.__module__.startswith("hypothesis"):
                return True
    return False


def _rel(root: Path | None, path: object) -> str | None:
    if path is None:
        return None
    p = Path(str(path))
    if root is not None:
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def _caller_frame(here: types.FrameType | None) -> types.FrameType | None:
    """The frame that called the function whose frame is ``here``."""
    return here.f_back if here is not None else None


_Node = TypeVar("_Node", bound="pytest.Item | pytest.Collector")


def _from_parent(node_type: type[_Node], parent: pytest.Collector, **kw: object) -> _Node:
    """``node_type.from_parent(parent, **kw)`` with a known result type."""
    node = node_type.from_parent(parent, **kw)  # pyright: ignore[reportUnknownMemberType]  # pytest leaves from_parent's keyword arguments untyped
    return cast("_Node", node)


def _is_state_machine(obj: type) -> bool:
    try:
        from hypothesis.stateful import RuleBasedStateMachine
    except Exception:  # pragma: no cover - hypothesis is a dependency
        return False
    return issubclass(obj, RuleBasedStateMachine)


def load_laws_fixtures(
    root: Path, pluginmanager: pytest.PytestPluginManager
) -> tuple[list[dict[str, str]], list[str]]:
    """Register the fixtures (only) of ``laws/conftest.py``; hooks there are ignored.

    Shared by the ``check`` and ``strength`` modes. Returns (collection errors as
    ``{"path", "message"}``, plain-English notes)."""
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    path = root / "laws" / "conftest.py"
    if not path.is_file():
        return errors, notes
    spec = importlib.util.spec_from_file_location("lawkeep_laws_conftest", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        return errors, notes
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules["lawkeep_laws_conftest"] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        errors.append(
            {
                "path": "laws/conftest.py",
                "message": f"laws/conftest.py could not be loaded: {type(exc).__name__}: {exc}",
            }
        )
        return errors, notes
    holder = types.ModuleType("lawkeep_laws_fixtures")
    hooks: list[str] = []
    for name, value in vars(module).items():
        if name.startswith("pytest_"):
            if name != "pytest_plugins":
                hooks.append(name)
            continue
        if name.startswith("__"):
            continue
        setattr(holder, name, value)
    if getattr(module, "pytest_plugins", None):
        notes.append(
            "laws/conftest.py sets pytest_plugins; the gate ignores it and loads only the "
            "fixtures defined in laws/conftest.py."
        )
    if hooks:
        notes.append(
            f"laws/conftest.py defines pytest hook(s) {', '.join(sorted(hooks))}; the gate "
            f"ignores hooks there and loads only its fixtures."
        )
    pluginmanager.register(holder, _LAWS_FIXTURES_NAME)
    return errors, notes


# ---------------------------------------------------------------------------------------------
# The per-law example database (replays the ledger)


class _LedgerDatabase(Protocol):
    """The per-law example database (a Hypothesis ``InMemoryExampleDatabase``)."""

    def new_failures(self) -> list[bytes]: ...


def _make_database(replay: list[bytes]) -> _LedgerDatabase:
    from hypothesis.database import InMemoryExampleDatabase

    class LedgerReplayDatabase(InMemoryExampleDatabase):
        """In-memory database whose main key also returns this law's saved failures."""

        def __init__(self, values: list[bytes]) -> None:
            super().__init__()
            self.replay = list(values)
            self.primary_keys: set[bytes] = set()

        @staticmethod
        def _is_primary(key: bytes) -> bool:
            return not key.endswith((b".secondary", b".pareto")) and b".hypothesis" not in key

        def fetch(self, key: bytes) -> Iterator[bytes]:
            seen: set[bytes] = set()
            if self._is_primary(key):
                self.primary_keys.add(key)
                for v in self.replay:
                    if v not in seen:
                        seen.add(v)
                        yield v
            for v in list(self.data.get(key, ())):
                if v not in seen:
                    seen.add(v)
                    yield v

        def save(self, key: bytes, value: bytes) -> None:
            if self._is_primary(key):
                self.primary_keys.add(key)
            super().save(key, value)

        def new_failures(self) -> list[bytes]:
            out: list[bytes] = []
            for key in self.primary_keys:
                for v in sorted(self.data.get(key, ()), key=lambda b: (len(b), b)):
                    if v not in self.replay and v not in out:
                        out.append(v)
            return out

    # ExampleDatabase's metaclass makes pyright see the base class as the constructed type.
    return cast("_LedgerDatabase", LedgerReplayDatabase(replay))


# ---------------------------------------------------------------------------------------------
# The active gate


class _LawRecord:
    def __init__(self, nodeid: str, law_id: str, sentence: str, file: str | None, kind: str):
        self.nodeid = nodeid
        self.law_id = law_id
        self.sentence = sentence
        self.file = file
        self.kind = kind
        self.is_hypothesis = False
        self.level = "L1"
        self.status = "not_run"
        self.message: str | None = None
        self.examples: list[dict[str, str]] = []
        self.duration = 0.0
        self.stats: dict[str, Any] | None = None
        self.explicit = 0
        self.seed: int | None = None
        self.replayed = 0
        self.db: _LedgerDatabase | None = None
        self.new_failures: list[bytes] = []
        self.exhausted: bool | None = None
        self.max_examples: int | None = None
        #: assume()/reject() calls that came from outside laws/ (e.g. the code under test)
        self.outside_filters = 0
        self.outside_filter_where: str | None = None
        #: a skip was raised while the law ran (not a skip marker)
        self.skip_raised = False


class _Gate:
    def __init__(self, config: pytest.Config, active: _Activation) -> None:
        self.config = config
        self.channel: ResultsChannel | None = active.channel
        self.root = active.root
        self.profile: dict[str, Any] = active.profile
        self.ledger_path = active.ledger
        self.profile_name = self.profile.get("name", "pr")
        self.examples = int(self.profile.get("examples", 200) or 200)
        self.derandomize = bool(self.profile.get("derandomize", True))
        self.backend = self.profile.get("backend", "hypothesis") or "hypothesis"
        self.deadline_ms = int(self.profile.get("deadline_ms", 0) or 0)
        self.seed: int | None = self.profile.get("seed")
        self.law_ids: set[str] | None = set(active.law_ids) if active.law_ids else None
        self.records: dict[str, _LawRecord] = {}  # nodeid -> record
        self.ignored: list[str] = []
        self.collection_errors: list[dict[str, str]] = []
        self.notes: list[str] = []
        self.started = time.monotonic()
        self._last_runner: object = None
        self._current: _LawRecord | None = None
        self._guard_filters()
        self._load_ledger()
        self._load_laws_fixtures()
        if self.backend == "crosshair":
            self._watch_runner()

    # -- setup -----------------------------------------------------------------------------

    def _guard_filters(self) -> None:
        """Notice assume()/reject() called from project code outside laws/.

        Only a law may decide which inputs it skips. Code under test that calls Hypothesis's
        assume(False) or reject() on the inputs it gets wrong would otherwise make a law hold.
        """
        try:
            import hypothesis
            import hypothesis.control as control
        except Exception:  # pragma: no cover
            return
        if getattr(control.assume, _GUARD_ATTR, False):
            return
        gate = self
        root = str(self.root)
        law_folder = str(self.root / "laws")

        def outside(frame: types.FrameType) -> str | None:
            try:
                f = os.path.abspath(frame.f_code.co_filename)
            except Exception:  # pragma: no cover
                return None
            if not f.startswith(root + os.sep) or f.startswith(law_folder + os.sep):
                return None
            if "site-packages" in f or "dist-packages" in f:
                return None
            return f"{_rel(gate.root, f)} line {frame.f_lineno}"

        def note(frame: types.FrameType | None) -> None:
            rec = gate._current
            where = outside(frame) if frame is not None else None
            if rec is not None and where is not None:
                rec.outside_filters += 1
                rec.outside_filter_where = rec.outside_filter_where or where

        orig_assume, orig_reject = control.assume, control.reject

        def assume(condition: object) -> bool:
            if not condition:
                note(_caller_frame(inspect.currentframe()))
            return orig_assume(condition)

        def reject() -> NoReturn:
            note(_caller_frame(inspect.currentframe()))
            orig_reject()

        assume.__doc__, reject.__doc__ = orig_assume.__doc__, orig_reject.__doc__
        replacements: dict[str, Callable[..., object]] = {"assume": assume, "reject": reject}
        for name, fn in replacements.items():
            setattr(fn, _GUARD_ATTR, True)
            for mod in (control, hypothesis):
                setattr(mod, name, fn)
        lk = sys.modules.get("lawkeep")
        if lk is not None and "assume" in vars(lk):
            setattr(lk, "assume", assume)  # noqa: B010 - replaces lawkeep's re-export too

    def _load_ledger(self) -> None:
        from lawkeep import ledger

        self.replay: dict[str, list[bytes]] = {}
        path_env = self.ledger_path
        if path_env == "":
            return
        try:
            path = Path(path_env) if path_env else self.root / "laws" / "failing_examples.jsonl"
            entries, problems = ledger.load_path(path)
        except Exception as exc:  # pragma: no cover - defensive
            self.notes.append(f"Could not read the saved failing examples: {exc}")
            return
        self.notes.extend(problems)
        self.replay = ledger.replay_values(entries)

    def _load_laws_fixtures(self) -> None:
        errors, notes = load_laws_fixtures(self.root, self.config.pluginmanager)
        self.collection_errors.extend(errors)
        self.notes.extend(notes)

    def _watch_runner(self) -> None:
        """Remember the last Hypothesis engine, to learn whether CrossHair explored all paths."""
        try:
            from hypothesis.internal.conjecture.engine import ConjectureRunner
        except Exception:  # pragma: no cover
            return
        gate = self
        original: Callable[..., object] = ConjectureRunner.run
        if getattr(original, _WRAPPED_ATTR, False):
            return

        def run(runner: ConjectureRunner, *args: object, **kwargs: object) -> object:
            gate._last_runner = runner
            return original(runner, *args, **kwargs)

        setattr(run, _WRAPPED_ATTR, True)
        setattr(ConjectureRunner, "run", run)  # noqa: B010 - wraps Hypothesis's engine method

    # -- collection ------------------------------------------------------------------------

    @staticmethod
    def _law_of(item: pytest.Item) -> LawMeta | None:
        from lawkeep._law import get_law

        obj: object = getattr(item, "obj", None)
        meta = get_law(obj) if obj is not None else None
        cls: object = getattr(item, "cls", None)
        if meta is None and cls is not None:
            meta = get_law(cls)
        return meta

    @pytest.hookimpl(tryfirst=True)
    def pytest_pycollect_makeitem(
        self, collector: pytest.Module | pytest.Class, name: str, obj: object
    ) -> _Collected:
        """Collect @law functions and classes whatever their names."""
        from lawkeep._law import get_law

        if name.startswith("__"):
            return None
        try:
            meta = get_law(obj)
        except Exception:
            return None
        if meta is None:
            return None
        if isinstance(obj, type):
            import unittest

            if _is_state_machine(obj):
                module: object = getattr(collector, "obj", None)
                test_case = cast("type[RuleBasedStateMachine]", obj).TestCase
                if module is not None and any(
                    v is test_case for v in vars(module).values() if isinstance(v, type)
                ):
                    return None  # collected through its exposed TestCase
                return self._unittest_item(collector, name, test_case)
            if issubclass(obj, unittest.TestCase):
                return None  # pytest's unittest support collects it whatever its name
            if collector.classnamefilter(name):
                return None
            cls = cast("type[object]", obj)  # isinstance() leaves it as type[Unknown]
            return _from_parent(pytest.Class, collector, name=name, obj=cls)
        if not callable(obj):
            return None
        if collector.funcnamefilter(name):
            return None  # the default collection handles it
        try:
            return list(collector._genfunctions(name, obj))  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType]  # pytest has no public API to build a function's items (parametrize included); falls back below
        except Exception:  # pragma: no cover - private API fallback
            return _from_parent(pytest.Function, collector, name=name, callobj=obj)

    @staticmethod
    def _unittest_item(
        collector: pytest.Module | pytest.Class, name: str, test_case: type
    ) -> pytest.Collector | None:
        try:
            from _pytest.unittest import UnitTestCase

            item = _from_parent(UnitTestCase, collector, name=name, obj=test_case)
            item.obj = test_case  # the module attribute `name` is the machine, not its TestCase
            return item
        except Exception:  # pragma: no cover - private API fallback
            return None

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            text = str(report.longrepr) if report.longrepr is not None else "unknown error"
            lines = [ln for ln in text.strip().splitlines() if ln.strip()]
            short = "\n".join(lines[-12:])
            self.collection_errors.append(
                {
                    "path": _rel(self.root, getattr(report, "fspath", None) or report.nodeid)
                    or report.nodeid,
                    "message": short,
                }
            )

    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        keep: list[pytest.Item] = []
        drop: list[pytest.Item] = []
        for item in items:
            meta = self._law_of(item)
            if meta is None:
                self.ignored.append(item.nodeid)
                drop.append(item)
                continue
            if self.law_ids is not None and meta.law_id not in self.law_ids:
                drop.append(item)
                continue
            keep.append(item)
            rec = _LawRecord(
                item.nodeid,
                meta.law_id,
                meta.sentence,
                _rel(self.root, getattr(item, "path", None) or getattr(item, "fspath", None)),
                meta.kind,
            )
            self.records[item.nodeid] = rec
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep

    # -- running ---------------------------------------------------------------------------

    def _settings_for(self, current: Settings, rec: _LawRecord) -> Settings:
        from hypothesis import Phase, settings

        own: int | None = getattr(current, "max_examples", None)
        default = settings.default.max_examples if settings.default is not None else None
        max_examples = self.examples
        if own is not None and own != default and own > self.examples:
            max_examples = own  # a law asking for more inputs keeps its own number
        kwargs: dict[str, Any] = {
            "max_examples": max_examples,
            "database": rec.db,
            "derandomize": False,
            "phases": tuple(Phase),
            "deadline": (self.deadline_ms or None),
            "print_blob": False,
        }
        if self.backend != "hypothesis":
            kwargs["backend"] = self.backend
        rec.max_examples = kwargs["max_examples"]
        return settings(current, **kwargs)

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        rec = self.records.get(item.nodeid)
        if rec is None:
            return
        self._current = rec
        rec.replayed = len(self.replay.get(rec.law_id, []))
        rec.db = _make_database(self.replay.get(rec.law_id, []))
        seed = None
        if self.derandomize:
            seed = stable_seed(rec.law_id)
        elif self.seed is not None:
            seed = int(self.seed)
        rec.seed = seed
        obj: object = getattr(item, "obj", None)
        fn: Any = getattr(
            obj, "__func__", obj
        )  # the test function; Hypothesis keeps its state on it
        machine: Any = None
        test_case: Any = getattr(item, "cls", None)
        run_test: object = getattr(test_case, "runTest", None) if test_case else None
        if run_test is not None:
            machine = getattr(run_test, "_hypothesis_state_machine_class", None)
        if machine is not None:
            rec.is_hypothesis = True
            test_case.settings = self._settings_for(test_case.settings, rec)
            if seed is not None:
                machine._hypothesis_internal_use_seed = seed
        elif fn is not None and getattr(fn, "hypothesis", None) is not None:
            rec.is_hypothesis = True
            from hypothesis import settings as Settings

            current = (
                getattr(fn, "_hypothesis_internal_use_settings", None)
                or Settings.default
                or Settings()
            )
            fn._hypothesis_internal_use_settings = self._settings_for(current, rec)
            if seed is not None:
                fn._hypothesis_internal_use_seed = seed
            rec.explicit = len(getattr(fn, "hypothesis_explicit_examples", []) or [])
        if rec.is_hypothesis:
            rec.level = "L3" if self.backend == "crosshair" else "L2"

    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        rec = self.records.get(item.nodeid)
        if rec is None or not rec.is_hypothesis:
            return (yield)
        from hypothesis.statistics import collector

        # Hypothesis declares its statistics hook as DynamicVariable(None); it holds a callback.
        stats_hook = cast("DynamicVariable[Callable[[dict[str, Any]], None] | None]", collector)

        def note(stats: dict[str, Any]) -> None:
            rec.stats = stats

        self._last_runner = None
        with stats_hook.with_value(note):
            try:
                return (yield)
            finally:
                runner = self._last_runner
                if runner is not None and self.backend == "crosshair":
                    rec.exhausted = bool(getattr(runner, "_verified_by_backend", None))
                if rec.seed is None:
                    obj: object = getattr(item, "obj", None)
                    fn: object = getattr(obj, "__func__", obj)
                    rec.seed = getattr(fn, "_hypothesis_internal_use_generated_seed", None)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        report = yield
        rec = self.records.get(item.nodeid)
        if rec is None:
            return report
        rec.duration += float(getattr(report, "duration", 0.0) or 0.0)
        exc = call.excinfo.value if call.excinfo is not None else None
        wasxfail = hasattr(report, "wasxfail")
        if call.when == "call":
            if report.passed:
                if rec.status == "not_run":
                    rec.status = "holds"
                if rec.outside_filters:
                    rec.status = "error"
                    rec.message = (
                        f"assume()/reject() was called {rec.outside_filters} time(s) from "
                        f"{rec.outside_filter_where}, outside laws/. That throws inputs away so "
                        f"the law never checks them. Only a law may filter its inputs, so this "
                        f"counts as an error, not a pass."
                    )
            elif report.skipped and not wasxfail:
                # pytest.skip() / unittest.SkipTest raised while the law ran (by the law or by
                # the code it calls). Skip markers act during set-up, never here.
                rec.status = "error"
                rec.message = (
                    "This law stopped itself with a skip while it was running ("
                    + (_skip_reason(report) or "no reason given")
                    + "). A skip raised by the law or by the code it calls is not a pass, so it "
                    "counts as an error. To skip a law on purpose, propose it with "
                    "`lawkeep propose` (a person must say yes)."
                )
                rec.skip_raised = True
            else:
                # failed, or xfailed (a failing law stays failing under xfail)
                if exc is not None and is_setup_error(exc):
                    rec.status = "error"
                    rec.message = (
                        "The law could not run properly (this is a problem with the law or "
                        "its input generators, not a counterexample): " + failure_text(exc)
                    )
                else:
                    rec.status = "fails"
                    rec.message = failure_text(exc) if exc is not None else _longrepr(report)
                    if wasxfail:
                        rec.message = (
                            "This law is marked xfail, but lawkeep reports a failing law as "
                            "failing. " + (rec.message or "")
                        )
                if exc is not None:
                    rec.examples = examples_from_exception(exc)
            if rec.db is not None and not rec.skip_raised:
                try:
                    rec.new_failures = rec.db.new_failures()
                except Exception:  # pragma: no cover
                    rec.new_failures = []
            self._current = None
        else:  # setup / teardown
            if report.skipped and call.when == "setup":
                if any(item.iter_markers(name="skip")) or any(item.iter_markers(name="skipif")):
                    rec.status = "skipped"
                    rec.message = _skip_reason(report)
                else:
                    rec.status = "error"
                    rec.message = (
                        "This law was skipped while it was being set up ("
                        + (_skip_reason(report) or "no reason given")
                        + "), not by a skip marker written on the law. That counts as an error."
                    )
            elif report.failed:
                rec.status = "error"
                what = "set up" if call.when == "setup" else "clean up after"
                detail = failure_text(exc) if exc is not None else _longrepr(report)
                rec.message = f"lawkeep could not {what} this law (fixture problem): {detail}"
        return report

    # -- results ---------------------------------------------------------------------------

    def _result_dicts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        by_law: dict[str, list[_LawRecord]] = {}
        for rec in self.records.values():
            by_law.setdefault(rec.law_id, []).append(rec)
        order = {"error": 0, "fails": 1, "not_run": 2, "skipped": 3, "holds": 4}
        results: list[dict[str, Any]] = []
        new_failures: list[dict[str, Any]] = []
        for law_id, recs in by_law.items():
            worst = min(recs, key=lambda r: order.get(r.status, 9))
            status = worst.status
            if status == "skipped" and any(r.status == "holds" for r in recs):
                status = "holds"
            tried = 0
            thrown_away = 0
            stop_reason = None
            for r in recs:
                if r.is_hypothesis:
                    thrown_away += _examples_invalid(r.stats) if r.stats else 0
                    tried += (_examples_tried(r.stats) if r.stats else 0) + r.explicit
                    if r.stats:
                        stop_reason = stop_reason or r.stats.get("stopped-because")
                elif r.status in ("holds", "fails"):
                    tried += 1
            example = worst.examples[0] if worst.examples else None
            evidence: dict[str, Any] = {
                "profile": self.profile_name,
                "backend": self.backend if worst.is_hypothesis else "pytest",
                "saved_failures_replayed": worst.replayed,
            }
            if worst.is_hypothesis:
                evidence.update(
                    {
                        "seed": worst.seed,
                        "derandomize": self.derandomize,
                        "max_examples": worst.max_examples,
                        "explicit_examples": worst.explicit,
                        "stop_reason": stop_reason,
                        "inputs_thrown_away": thrown_away,
                    }
                )
                if self.backend == "crosshair":
                    evidence["exhausted"] = bool(worst.exhausted)
                    evidence["bounded"] = not bool(worst.exhausted)
            if len(worst.examples) > 1:
                evidence["other_failing_examples"] = worst.examples[1:]
            if len(recs) > 1:
                evidence["cases"] = len(recs)
            results.append(
                {
                    "law_id": law_id,
                    "sentence": worst.sentence,
                    "status": status,
                    "level": worst.level,
                    "examples_tried": tried,
                    "simplest_failing_example": example,
                    "failure_message": worst.message,
                    "duration_seconds": round(sum(r.duration for r in recs), 4),
                    "evidence": evidence,
                    "file": worst.file,
                    "nodeid": worst.nodeid,
                }
            )
            from lawkeep import ledger

            for r in recs:
                first_line = (r.message or "").strip().splitlines()
                for value in r.new_failures:
                    new_failures.append(
                        {
                            "law_id": law_id,
                            "choices": ledger.encode_choices(value),
                            "example": (
                                r.examples[0] if len(r.new_failures) == 1 and r.examples else None
                            ),
                            "failure": first_line[0] if first_line else None,
                        }
                    )
        return results, new_failures

    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: int | pytest.ExitCode
    ) -> None:
        self._write_results(exitstatus)

    def _write_results(self, exitstatus: int | pytest.ExitCode) -> None:
        """Results JSON: {version, results [LawResult dict], new_failures, ignored,
        collection_errors, notes, pytest_exit_status, duration_seconds}."""
        results, new_failures = self._result_dicts()
        payload = {
            "version": RESULTS_VERSION,
            "results": results,
            "new_failures": new_failures,
            "ignored": self.ignored,
            "collection_errors": self.collection_errors,
            "notes": self.notes,
            "pytest_exit_status": int(exitstatus),
            "duration_seconds": round(time.monotonic() - self.started, 4),
        }
        channel, self.channel = self.channel, None
        if channel is not None:
            # Sent once, then the channel is closed: code that runs later in this process (an
            # atexit hook, say) cannot change what the parent reads.
            channel.send({"type": "check", **payload})
            channel.close()


def _phase_cases(stats: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    """The test cases Hypothesis's statistics list for one phase (``[]`` when missing)."""
    phase_stats: dict[str, Any] = stats.get(phase) or {}
    cases: list[dict[str, Any]] = phase_stats.get("test-cases") or []
    return cases


def _examples_tried(stats: dict[str, Any]) -> int:
    """Inputs actually run in the reuse (saved failures) and generate phases."""
    total = 0
    for phase in ("reuse-phase", "generate-phase"):
        cases = _phase_cases(stats, phase)
        total += sum(1 for c in cases if c.get("status") in ("valid", "interesting"))
    return total


def _examples_invalid(stats: dict[str, Any]) -> int:
    """Generated inputs thrown away (assume()/filter()/reject(), or too big to finish)."""
    cases = _phase_cases(stats, "generate-phase")
    return sum(1 for c in cases if c.get("status") in ("invalid", "overrun"))


def _skip_reason(report: pytest.TestReport) -> str | None:
    lr = report.longrepr
    if isinstance(lr, tuple) and len(lr) == 3:
        reason = str(lr[2])
        return reason[len("Skipped: ") :] if reason.startswith("Skipped: ") else reason
    return str(lr) if lr else None


def _longrepr(report: pytest.TestReport) -> str:
    text = str(report.longrepr or "").strip()
    lines = text.splitlines()
    return "\n".join(lines[-12:])
