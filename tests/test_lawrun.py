"""The law-running core (``lawkeep.lawrun``): one way to run laws, three modes.

Each mode is started the same way and must keep the same isolation; each mode's caller must
keep its own results. Made-up project only (a tiny "parcels" shop written here).
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from lawkeep import config, examples_table, lawrun, runner
from lawkeep.strength import core

ISOLATION_LAW = """
import os
import sys

from lawkeep import law


@law("A law runs in lawkeep's isolated process")
def runs_isolated():
    import parcels

    assert parcels.weight_band(3) == "small"
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    assert "sitecustomize" not in sys.modules, "sitecustomize.py ran"
    assert "shadow_from_pythonpath" not in sys.modules, "PYTHONPATH was used"
    assert os.environ.get("PYTHONPATH") is None
    assert os.environ.get("PYTEST_ADDOPTS") is None
    assert os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"
    left = sorted(k for k in os.environ if k.startswith("LAWKEEP_"))
    assert left == ["LAWKEEP_ROOT"], left
    assert not os.environ.get("ROOT_CONFTEST_RAN"), "the project's own conftest.py ran"
    assert not os.environ.get("LAWS_CONFTEST_HOOK_RAN"), "a hook in laws/conftest.py ran"
    root = os.environ["LAWKEEP_ROOT"]
    storage = os.environ.get("HYPOTHESIS_STORAGE_DIRECTORY", "")
    assert storage and not storage.startswith(root), storage
    assert os.read(0, 100) == b"", "stdin still holds the request"


@law("The hash seed is fixed")
def hash_seed_is_fixed():
    assert os.environ.get("PYTHONHASHSEED") == "0"
"""


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "parcels"
    files = {
        "lawkeep.toml": '[agent]\nname = "other"\n',
        "src/parcels/__init__.py": """
            def weight_band(kg: int) -> str:
                return "small" if kg < 5 else "large"
        """,
        "laws/__init__.py": "",
        "laws/isolation.py": ISOLATION_LAW,
        # Traps: each one would break the isolation law if it ran or was used.
        "sitecustomize.py": "import os\nos.environ['SITECUSTOMIZE'] = '1'\n",
        "conftest.py": "import os\nos.environ['ROOT_CONFTEST_RAN'] = '1'\n",
        "laws/conftest.py": """
            import os
            import pytest


            @pytest.fixture
            def parcel_weight():
                return 3


            def pytest_collection_modifyitems(items):
                os.environ["LAWS_CONFTEST_HOOK_RAN"] = "1"
        """,
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


@pytest.fixture
def hostile_env(tmp_path, monkeypatch):
    """A caller's shell full of things that must not reach a law-running process."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "shadow_from_pythonpath.py").write_text("X = 1\n")
    (shadow / "sitecustomize.py").write_text("import shadow_from_pythonpath\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:nonexistent_plugin_xyz")
    monkeypatch.setenv("LAWKEEP_EXTRA_SYS_PATH", str(shadow))
    monkeypatch.setenv("LAWKEEP_RESULTS_FILE", str(tmp_path / "forged.json"))
    monkeypatch.setenv("PYTHONHASHSEED", "123")
    return shadow


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _statuses_check(root: Path) -> dict[str, str]:
    out = runner.run_laws(root, config.load_config(root))
    return {r.law_id: f"{r.status}: {r.failure_message}" for r in out.results}


def _statuses_strength(root: Path) -> dict[str, str]:
    run = core.run_worker(
        root,
        config.load_config(root),
        ["laws/isolation.py"],
        {"kind": "map", "law_ids": None, "examples": 5, "timeout": 60},
        timeout=300,
    )
    assert run.finished, run.stderr
    return {r["law_id"]: f"{r['status']}: {r.get('message')}" for r in run.of_type("law")}


def _statuses_probe(root: Path) -> dict[str, str]:
    cfg = config.load_config(root)
    out: dict[str, str] = {}
    for name in ("runs_isolated", "hash_seed_is_fixed"):
        data = examples_table.run_probe(
            root,
            cfg,
            [{"name": "v", "file": str(root / "laws" / "isolation.py"), "qualname": name}],
            examples=5,
        )
        assert "error" not in data, data
        real = data["cases"][0]["real"]["v"]
        out[f"isolation.{name}"] = f"{real['outcome']}: {real.get('message')}"
    return out


RUNNERS = {"check": _statuses_check, "strength": _statuses_strength, "probe": _statuses_probe}


@pytest.mark.parametrize("mode", ["check", "strength", "probe"])
def test_every_mode_is_isolated(tmp_path, hostile_env, mode):
    root = make_project(tmp_path)
    before = snapshot(root)
    statuses = RUNNERS[mode](root)
    assert statuses["isolation.runs_isolated"].startswith("holds"), statuses
    # PYTHONHASHSEED=0 is part of strength's and probe's determinism; the gate keeps the
    # caller's value (unchanged behaviour), so with 123 in the shell that law fails there.
    seed = statuses["isolation.hash_seed_is_fixed"]
    assert seed.startswith("fails" if mode == "check" else "holds"), statuses
    assert snapshot(root) == before, "a law-running process wrote into the project"
    assert not (tmp_path / "forged.json").exists()


def test_run_sends_the_request_on_stdin_and_reads_records(tmp_path):
    root = make_project(tmp_path)
    run = lawrun.run(
        root,
        config.load_config(root),
        "probe",
        {
            "probe": {
                "laws_dir": str(root / "laws"),
                "versions": [
                    {
                        "name": "v",
                        "file": str(root / "laws" / "isolation.py"),
                        "qualname": "runs_isolated",
                    }
                ],
                "examples": 3,
                "mutants": False,
                "extra_inputs": [],
                "strategies_override": None,
                "seed": 1,
            }
        },
        timeout=300,
    )
    assert [r["type"] for r in run.records] == ["probe", "done"]
    assert run.unreadable == [] and run.finished and not run.timed_out
    assert run.of_type("probe")[0]["result"]["versions"]["v"]["status"] == "ok"


def test_pytest_modes_share_the_same_base_arguments(tmp_path):
    ini = tmp_path / "pytest.ini"
    gate = lawrun.pytest_args(tmp_path, ini, ["laws/a.py"], plugins=runner.GATE_PLUGINS)
    strength = lawrun.pytest_args(tmp_path, ini, ["laws/a.py"], extra=core.STRENGTH_PYTEST_OPTIONS)
    base = [
        "-c",
        str(ini),
        f"--rootdir={tmp_path.resolve()}",
        "--noconftest",
        "--import-mode=importlib",
        "--continue-on-collection-errors",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    assert gate[: len(base)] == base and strength[: len(base)] == base
    assert gate[-1] == strength[-1] == "laws/a.py"
    assert "lawkeep.pytest_plugin" in gate and "lawkeep.pytest_plugin" not in strength


def test_command_is_the_isolated_child(tmp_path):
    argv = lawrun.python_command(tmp_path / "pyc")
    assert argv[:5] == [sys.executable, "-I", "-S", "-B", "-X"]
    assert argv[5] == f"pycache_prefix={tmp_path / 'pyc'}"
    assert argv[6] == str(lawrun.BOOT_SCRIPT)
    assert json.loads(argv[7]) == lawrun.site_dirs()
    assert argv[8:] == ["lawkeep._lawrun_child"]


def test_check_mode_closes_the_channel_after_its_one_record(tmp_path):
    """Code that runs after the gate wrote its results (an atexit hook) cannot add a record."""
    root = make_project(tmp_path)
    (root / "src" / "parcels" / "__init__.py").write_text(
        textwrap.dedent(
            """
            import atexit, os

            def _late():
                for fd in range(3, 64):
                    try:
                        os.write(fd, b'{"type": "check", "results": []}\\n')
                    except OSError:
                        pass

            atexit.register(_late)

            def weight_band(kg: int) -> str:
                return "small" if kg < 5 else "large"
            """
        ),
        encoding="utf-8",
    )
    run = lawrun.run(
        root,
        config.load_config(root),
        "check",
        {"gate": {"profile": {"examples": 5}, "law_ids": []}},
        pytest_targets=["laws/isolation.py"],
        pytest_plugins=runner.GATE_PLUGINS,
        timeout=300,
    )
    assert len(run.of_type("check")) == 1 and run.unreadable == []
    assert {r["law_id"] for r in run.of_type("check")[0]["results"]} >= {"isolation.runs_isolated"}


def test_strength_keeps_partial_records_when_stopped(tmp_path):
    root = make_project(tmp_path)
    (root / "laws" / "slow.py").write_text(
        textwrap.dedent(
            """
            import time
            from lawkeep import law

            @law("A quick law")
            def a_quick():
                assert True

            @law("A law that never ends")
            def z_endless():
                time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    run = core.run_worker(
        root,
        config.load_config(root),
        ["laws/slow.py"],
        {"kind": "map", "law_ids": None, "examples": 5, "timeout": 120},
        timeout=8,
    )
    assert run.timed_out and not run.finished
    assert [r["law_id"] for r in run.of_type("law")] == ["slow.a_quick"]
    assert (
        core.worker_problem(run) == "the strength check ran out of time; results below are partial"
    )


def test_unknown_mode_is_a_fatal_record(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setitem(lawrun.MODES, "nonsense", lawrun.MODES["probe"])  # pyright: ignore[reportArgumentType]  # deliberately unknown
    run = lawrun.run(root, config.load_config(root), "nonsense", {}, timeout=120)  # pyright: ignore[reportArgumentType]  # deliberately unknown
    fatal = run.first("fatal")
    assert fatal is not None and "KeyError" in fatal["message"]
    assert "KeyError" in run.stderr


def test_start_failure_is_a_setup_problem(tmp_path, monkeypatch):
    from lawkeep.errors import SetupError

    root = make_project(tmp_path)
    monkeypatch.setattr(lawrun.sys, "executable", str(tmp_path / "no-such-python"))
    with pytest.raises(SetupError, match="could not start Python to measure your laws"):
        core.run_worker(root, config.load_config(root), ["laws/isolation.py"], {"kind": "map"})


def test_no_lawkeep_variable_reaches_the_child_environment(monkeypatch):
    monkeypatch.setenv("LAWKEEP_EXTRA_SYS_PATH", "/x")
    monkeypatch.setenv("LAWKEEP_RESULTS_FD", "7")
    env = lawrun.clean_env({"LAWKEEP_ROOT": "/r"})
    assert sorted(k for k in env if k.startswith("LAWKEEP_")) == ["LAWKEEP_ROOT"]
    assert os.environ["LAWKEEP_RESULTS_FD"] == "7"  # the caller's own environment is untouched
