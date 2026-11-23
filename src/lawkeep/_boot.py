"""Start-up script for every law-running process (all three modes of ``lawkeep.lawrun``).

Run as ``python -I -S -B -X pycache_prefix=<tmp> _boot.py <site dirs JSON> <module> args...``
(lawkeep always starts ``lawkeep._lawrun_child``; any module works, which the tests use).

Why: ``-S`` stops Python from importing ``site`` at start-up, so no ``sitecustomize.py``,
``usercustomize.py`` or code line in a ``*.pth`` file runs before (or inside) the laws. Python
then does not know where installed packages live, so this script adds the site-packages
folders it is given and the plain folder lines of their ``.pth`` files (that is how editable
installs and uv/pip path entries work). Lines starting with ``import`` are NOT run, except the
fixed form setuptools writes for editable installs (``import __editable___x_finder;
__editable___x_finder.install(...)``), which only registers an import finder for that
installed package.

Only the standard library is used here: nothing else is importable yet.
"""

from __future__ import annotations

import json
import os
import re
import sys

_EDITABLE_FINDER = re.compile(
    r"^import (__editable___[A-Za-z0-9_]+_finder)\s*;\s*\1\.install\(\s*['\"][^'\"\n]*['\"]\s*\)\s*$"
)


def _add_site_dir(sitedir: str) -> None:
    if not sitedir or not os.path.isdir(sitedir):
        return
    if sitedir not in sys.path:
        sys.path.append(sitedir)
    try:
        names = sorted(os.listdir(sitedir))
    except OSError:
        return
    for name in names:
        if not name.endswith(".pth") or name.startswith("."):
            continue
        try:
            with open(os.path.join(sitedir, name), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("import ", "import\t")):
                if _EDITABLE_FINDER.match(stripped):
                    try:
                        exec(stripped, {})  # noqa: S102 - fixed setuptools editable-install form
                    except Exception:
                        pass
                continue
            path = os.path.abspath(os.path.join(sitedir, line.rstrip()))
            if os.path.isdir(path) and path not in sys.path:
                sys.path.append(path)


def main() -> None:
    site_dirs = json.loads(sys.argv[1])
    module = sys.argv[2]
    for d in site_dirs:
        _add_site_dir(d)
    sys.argv = [module, *sys.argv[3:]]
    import runpy

    runpy.run_module(module, run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
