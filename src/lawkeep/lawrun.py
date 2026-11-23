"""The law-running core: the one way lawkeep runs laws (parent side).

Every command that runs project code goes through :func:`run`, in one of three modes:

``check``     the gate (``lawkeep check``): pytest with lawkeep's gate plugin
              (``lawkeep.pytest_plugin.run_check``); one result per law.
``strength``  ``lawkeep strength``, the protection report and ``propose``'s planted bugs:
              pytest with the strength plugin (``lawkeep.strength._worker.run``): coverage per
              law, in-memory function swaps and planted bugs.
``probe``     example tables and direction evidence (``draft``, ``approve``, ``propose``):
              Hypothesis without pytest (``lawkeep._probe_worker.run``), recording each input
              and result.

What every mode shares (so the isolation rules exist once):

- **The command.** ``python -I -S -B -X pycache_prefix=<empty temp folder> _boot.py <site
  dirs> lawkeep._lawrun_child``, cwd = project root. ``-I`` ignores PYTHONPATH, other
  PYTHON* variables, the user site folder and the current folder; ``-S`` plus ``_boot.py``
  means no ``sitecustomize.py``, ``usercustomize.py`` or code line in a ``.pth`` file runs
  (``_boot.py`` adds the installed packages' folders itself); ``-B`` and the empty
  ``pycache_prefix`` mean no bytecode is written and every ``__pycache__/*.pyc`` in the
  project is ignored.
- **The environment** (:func:`clean_env`): without variables that could load extra pytest
  plugins or options and without any ``LAWKEEP_*`` variable of the caller (a
  ``LAWKEEP_EXTRA_SYS_PATH`` left in an agent's shell cannot swap in other code); only
  ``LAWKEEP_ROOT`` is set (``lawkeep.st`` needs it), plus ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1``,
  ``PYTHONDONTWRITEBYTECODE=1``, a private ``HYPOTHESIS_STORAGE_DIRECTORY`` in the temp
  folder (nothing is written into the project, no cache from an earlier run is reused) and,
  for ``strength`` and ``probe``, ``PYTHONHASHSEED=0`` (:data:`MODES`).
- **The request** goes in once, as JSON on the child's stdin (which is then switched to
  ``/dev/null``): mode, root, ``sys_path`` (extra folders, the project's source folders, then
  the root), the pytest arguments for pytest modes (:func:`pytest_args`: an empty ini file,
  ``--noconftest``, ``--import-mode=importlib``, no cache provider) and the mode's own fields.
- **The results** come back through a private pipe (POSIX; a results file in the temp folder
  elsewhere) as JSON lines. The child closes it when the run is done (the gate closes it as
  soon as its one results record is written), so code that runs later in that process (an
  ``atexit`` hook in the code under test, say) cannot rewrite them. The pipe's number is
  passed in the request, never in the environment. Records arrive as they are written, so a
  run stopped by its time limit still returns what it found so far.

This is a speed bump, not a wall: the code under test runs in the same process as the law
runner and is trusted (see the README, "Known limits").
"""

from __future__ import annotations

import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from lawkeep.errors import SetupError

if TYPE_CHECKING:
    from lawkeep.config import Config

Mode = Literal["check", "strength", "probe"]

BOOT_SCRIPT = Path(__file__).with_name("_boot.py")
#: The module every law-running process starts (through ``_boot.py``).
CHILD_MODULE = "lawkeep._lawrun_child"

#: Variables never passed to a law-running process (every ``LAWKEEP_*`` variable is dropped too).
DROPPED_ENV = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "COV_CORE_SOURCE",
    "COVERAGE_PROCESS_START",
)


@dataclass(frozen=True)
class ModeSpec:
    """What differs between modes on the parent side (the child's handlers are listed in
    ``lawkeep._lawrun_child``)."""

    #: Set ``PYTHONHASHSEED=0`` (set and dict-of-str ordering in generators, and in the
    #: constants Hypothesis collects from source, must not change the inputs between runs).
    fixed_hash_seed: bool
    #: Finishes "lawkeep could not start Python to ..." when the interpreter cannot start.
    purpose: str
    hint: str | None = None


MODES: dict[Mode, ModeSpec] = {
    "check": ModeSpec(
        fixed_hash_seed=False,
        purpose="run the laws",
        hint="Check that lawkeep is installed in the project's environment.",
    ),
    "strength": ModeSpec(
        fixed_hash_seed=True,
        purpose="measure your laws",
        hint="Check that the Python running lawkeep still exists.",
    ),
    "probe": ModeSpec(fixed_hash_seed=True, purpose="run the law on examples"),
}


class ResultsChannel(Protocol):
    """The child's end of the results channel (``lawkeep._lawrun_child.Channel``)."""

    def send(self, record: dict[str, Any], *, default: Any = str) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------------------------
# The command, the environment and the pytest arguments


def site_dirs() -> list[str]:
    """The installed-packages folders of the Python running lawkeep (passed to ``_boot.py``)."""
    out: list[str] = []
    candidates: list[str] = []
    try:
        candidates += list(site.getsitepackages())
    except AttributeError:  # pragma: no cover - very old virtualenv
        pass
    for key in ("purelib", "platlib"):
        try:
            candidates.append(sysconfig.get_paths()[key])
        except KeyError:  # pragma: no cover
            pass
    # Folders already on sys.path that are site folders (covers unusual layouts).
    candidates += [p for p in sys.path if p.endswith(("site-packages", "dist-packages"))]
    for c in candidates:
        try:
            r = str(Path(c).resolve())
        except OSError:  # pragma: no cover
            continue
        if r not in out and Path(r).is_dir():
            out.append(r)
    return out


def python_command(pycache_dir: Path | str, module: str = CHILD_MODULE) -> list[str]:
    """``[python, -I, -S, -B, -X pycache_prefix=..., _boot.py, <site dirs>, module]``."""
    return [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-X",
        f"pycache_prefix={pycache_dir}",
        str(BOOT_SCRIPT),
        json.dumps(site_dirs()),
        module,
    ]


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for a law-running process (see the module docstring)."""
    env = {
        k: v for k, v in os.environ.items() if k not in DROPPED_ENV and not k.startswith("LAWKEEP_")
    }
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for k, v in (extra or {}).items():
        env[k] = v
    return env


def sys_path_entries(root: Path, cfg: Config) -> list[str]:
    """Folders put first on the child's ``sys.path``: the source folders, then the root."""
    out: list[str] = []
    for d in [*cfg.project.source_dirs(root), Path(root).resolve()]:
        s = str(d)
        if Path(s).is_dir() and s not in out:
            out.append(s)
    return out


def pytest_args(
    root: Path,
    ini_file: Path,
    targets: list[str],
    *,
    plugins: list[str] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """The pytest arguments of every pytest mode: an empty ini file, the project root as
    rootdir, no conftest.py (``laws/conftest.py``'s fixtures are loaded by lawkeep), no
    sys.path changes by pytest, no cache; then ``-p`` for each plugin, ``extra`` and the
    targets."""
    args = [
        "-c",
        str(ini_file),
        f"--rootdir={Path(root).resolve()}",
        "--noconftest",
        "--import-mode=importlib",
        "--continue-on-collection-errors",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    for plugin in plugins or []:
        args += ["-p", plugin]
    return args + list(extra or []) + list(targets)


# ---------------------------------------------------------------------------------------------
# Running one request


@dataclass
class Captured:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    #: What the child wrote to its results channel.
    data: bytes = b""


def pipe_supported() -> bool:
    return os.name == "posix"


def run_capture(
    argv: list[str],
    *,
    cwd: Path | str,
    env: dict[str, str],
    request: dict[str, Any],
    results_file: Path | None = None,
    timeout: float | None = None,
) -> Captured:
    """Start ``argv``, send ``request`` (JSON) on its stdin and collect its output and results.

    The results channel is a pipe whose number is added to the request as ``results_fd``
    (POSIX), or else ``results_file`` (added as ``results_file``). Raises OSError when the
    process cannot start. On timeout the child is killed and ``timed_out`` is set; whatever
    it wrote to the channel until then is kept.
    """
    read_fd = write_fd = None
    pass_fds: tuple[int, ...] = ()
    request = dict(request)
    if pipe_supported():
        read_fd, write_fd = os.pipe()
        request["results_fd"] = write_fd
        pass_fds = (write_fd,)
    elif results_file is not None:
        request["results_file"] = str(results_file)
    payload = json.dumps(request).encode("utf-8")
    chunks: list[bytes] = []
    reader = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=pass_fds,
        )
    except BaseException:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        raise
    if write_fd is not None:
        os.close(write_fd)  # only the child holds the write end now
        assert read_fd is not None

        def _read(fd: int = read_fd) -> None:
            with os.fdopen(fd, "rb") as f:
                while True:
                    block = f.read(65536)
                    if not block:
                        break
                    chunks.append(block)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
    timed_out = False
    try:
        out, err = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    if reader is not None:
        reader.join(timeout=10)
        data = b"".join(chunks)
    elif results_file is not None and results_file.is_file():
        data = results_file.read_bytes()
    else:
        data = b""
    return Captured(
        returncode=proc.returncode if not timed_out else -1,
        stdout=(out or b"").decode("utf-8", "replace"),
        stderr=(err or b"").decode("utf-8", "replace"),
        timed_out=timed_out,
        data=data,
    )


@dataclass
class LawRunResult:
    """What one law-running process returned."""

    mode: Mode
    records: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: Lines on the results channel that were not one JSON object each.
    unreadable: list[str] = field(default_factory=list[str])

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.records if r.get("type") == kind]

    def first(self, kind: str) -> dict[str, Any] | None:
        found = self.of_type(kind)
        return found[0] if found else None

    @property
    def finished(self) -> bool:
        """The child got to the end of its run (it wrote its "done" record)."""
        return any(r.get("type") == "done" for r in self.records)

    def output_tail(self, lines: int) -> str:
        """The last ``lines`` lines the child printed (stderr first), for error messages."""
        return "\n".join((self.stderr or self.stdout or "").strip().splitlines()[-lines:])


def parse_records(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """Split the results channel's JSON lines into records and unreadable lines."""
    records: list[dict[str, Any]] = []
    bad: list[str] = []
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec: object = json.loads(line)
        except ValueError:
            bad.append(line[:200])
            continue
        if isinstance(rec, dict):
            records.append(cast("dict[str, Any]", rec))  # JSON object keys are strings
        else:
            bad.append(line[:200])
    return records, bad


def run(
    root: Path,
    cfg: Config,
    mode: Mode,
    request: dict[str, Any] | None = None,
    *,
    pytest_targets: list[str] | None = None,
    pytest_plugins: list[str] | None = None,
    pytest_extra: list[str] | None = None,
    extra_sys_path: list[str] | None = None,
    timeout: float | None = None,
) -> LawRunResult:
    """Run one request in a fresh isolated process (see the module docstring).

    ``request`` holds the mode's own fields. For the pytest modes (``check``, ``strength``)
    give ``pytest_targets`` (law files relative to ``root``), and optionally plugins and
    extra arguments; the request then carries ``pytest_args``. ``extra_sys_path`` folders go
    before the project's source folders (e.g. a test plugin). Never raises for failing laws;
    raises SetupError only when Python cannot start.
    """
    root = Path(root).resolve()
    spec = MODES[mode]
    tmp = Path(tempfile.mkdtemp(prefix=f"lawkeep-{mode}-"))
    try:
        pycache = tmp / "pycache"
        pycache.mkdir()
        sys_path: list[str] = []
        for p in [*(extra_sys_path or []), *sys_path_entries(root, cfg)]:
            if p and p not in sys_path:
                sys_path.append(p)
        full: dict[str, Any] = {
            **(request or {}),
            "mode": mode,
            "root": str(root),
            "work_dir": str(tmp),
            "sys_path": sys_path,
            "source_dirs": [str(d) for d in cfg.project.source_dirs(root)],
        }
        if pytest_targets is not None:
            ini = tmp / "pytest.ini"
            ini.write_text("[pytest]\n", encoding="utf-8")
            full["pytest_args"] = pytest_args(
                root, ini, pytest_targets, plugins=pytest_plugins, extra=pytest_extra
            )
        extra_env = {
            "LAWKEEP_ROOT": str(root),
            "HYPOTHESIS_STORAGE_DIRECTORY": str(tmp / "hypothesis"),
        }
        if spec.fixed_hash_seed:
            extra_env["PYTHONHASHSEED"] = "0"
        try:
            cap = run_capture(
                python_command(pycache),
                cwd=root,
                env=clean_env(extra_env),
                request=full,
                results_file=tmp / "results.jsonl",
                timeout=timeout,
            )
        except OSError as exc:
            raise SetupError(
                f"lawkeep could not start Python to {spec.purpose} ({exc}).", hint=spec.hint
            ) from None
        records, bad = parse_records(cap.data)
        return LawRunResult(
            mode=mode,
            records=records,
            returncode=cap.returncode,
            stdout=cap.stdout,
            stderr=cap.stderr,
            timed_out=cap.timed_out,
            unreadable=bad,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
