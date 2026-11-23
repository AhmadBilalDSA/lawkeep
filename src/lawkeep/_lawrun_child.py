"""The child side of the law-running core (see :mod:`lawkeep.lawrun`).

Started by ``lawkeep.lawrun.run`` as ``python -I -S -B -X pycache_prefix=<tmp> _boot.py <site
dirs> lawkeep._lawrun_child`` with cwd = project root. Before any law or project code is
imported it:

1. reads the request (one JSON object) from stdin, then points stdin at ``/dev/null``;
2. opens the results channel named in the request (``results_fd``, an inherited pipe, or
   ``results_file``);
3. removes every ``LAWKEEP_*`` variable except ``LAWKEEP_ROOT`` from ``os.environ``;
4. puts the request's ``sys_path`` first on ``sys.path`` and turns bytecode writing off;

then hands the request and the channel to the mode's handler (:data:`HANDLERS`) and closes
the channel when the handler returns. A crash in lawkeep's own code is sent as a ``fatal``
record and printed to stderr.

Only the standard library is imported here before the handler runs.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from typing import Any

#: mode -> (module, function). Each handler takes (request, channel) and returns an exit code.
HANDLERS: dict[str, tuple[str, str]] = {
    "check": ("lawkeep.pytest_plugin", "run_check"),
    "strength": ("lawkeep.strength._worker", "run"),
    "probe": ("lawkeep._probe_worker", "run"),
}


class Channel:
    """Results as JSON lines on a pipe or in a file. After ``close`` nothing more is sent."""

    def __init__(self, fd: int | None = None, path: str | None = None) -> None:
        self._fd = fd
        self._file = open(path, "a", encoding="utf-8") if path and fd is None else None  # noqa: SIM115 - closed in close()
        self.closed = fd is None and self._file is None

    def send(self, record: dict[str, Any], *, default: Any = str) -> None:
        if self.closed:
            return
        line = json.dumps(record, ensure_ascii=False, default=default) + "\n"
        if self._fd is not None:
            view = memoryview(line.encode("utf-8"))
            while view:
                n = os.write(self._fd, view)
                view = view[n:]
        elif self._file is not None:
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        if self._file is not None:
            self._file.close()
            self._file = None


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(devnull, 0)
    finally:
        os.close(devnull)
    sys.stdin = open(0, encoding="utf-8", closefd=False)  # noqa: SIM115 - stays open for the process
    request: dict[str, Any] = json.loads(raw.decode("utf-8"))
    return request


def _scrub_environment() -> None:
    for key in list(os.environ):
        if key.startswith("LAWKEEP_") and key != "LAWKEEP_ROOT":
            del os.environ[key]


def _setup_sys_path(entries: list[str]) -> None:
    for p in reversed(entries):
        if p in sys.path:
            sys.path.remove(p)
        sys.path.insert(0, p)
    sys.dont_write_bytecode = True


def main() -> int:
    request = _read_request()
    fd = request.get("results_fd")
    channel = Channel(fd=fd if isinstance(fd, int) else None, path=request.get("results_file"))
    _scrub_environment()
    entries: list[object] = request.get("sys_path") or []
    _setup_sys_path([str(p) for p in entries])
    try:
        module_name, func_name = HANDLERS[str(request.get("mode"))]
        handler = getattr(importlib.import_module(module_name), func_name)
        return int(handler(request, channel))
    except BaseException as exc:  # reported to the parent, which decides what it means
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        first = next((ln.strip() for ln in str(exc).strip().splitlines() if ln.strip()), "")
        channel.send(
            {
                "type": "fatal",
                "message": f"{type(exc).__name__}: {first}"[:300],
                "traceback": tb[-3000:],
            }
        )
        return 3
    finally:
        channel.close()


if __name__ == "__main__":  # pragma: no cover - started through _boot.py
    sys.exit(main())
