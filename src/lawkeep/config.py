"""Load and validate ``lawkeep.toml``.

Schema (every key optional; defaults shown)::

    [permissions]            # what an agent may do to laws without asking: "auto" or "ask"
    add_law       = "auto"   # add a new law                       (stricter)
    add_example   = "auto"   # add an example / saved failing case (stricter)
    rename_target = "auto"   # update a law after a rename, same meaning confirmed
    change_law    = "ask"    # change what a law checks            (unclear)
    weaken_law    = "ask"    # loosen, delete, skip, fewer cases   (always "ask"; fixed)

    [check]
    profile = "pr"           # profile used when `lawkeep check` gets no --profile

    [check.pr]               # pull requests and everyday runs: same code -> same verdict
    examples    = 200        # Hypothesis max_examples per law
    derandomize = true       # fixed seed
    backend     = "hypothesis"   # or "crosshair" (L3, experimental; needs lawkeep[deep])
    deadline_ms = 0          # 0 = no per-example deadline

    [check.deep]             # nightly / on demand: deeper random search
    examples    = 2000
    derandomize = false
    backend     = "hypothesis"
    deadline_ms = 0

    [project]
    source = ["src"]         # where the code under test lives (default: "src" if it exists, else ".")

    [strength]
    mutation_tool   = "mutmut"
    quick_check     = true
    min_bugs_caught = 1      # a law catching fewer planted bugs is flagged

    [agent]                  # how `lawkeep draft` starts your coding agent non-interactively
    name    = ""             # "claude-code" | "codex" | "cursor" | "copilot" | "other" | ""
    command = ""             # e.g. "claude -p --permission-mode acceptEdits {prompt}"
    timeout_seconds = 900

``weaken_law = "auto"`` is not allowed: lawkeep keeps it at "ask" and adds a warning.
Unknown keys produce warnings (with a "did you mean" suggestion), wrong types raise
:class:`~lawkeep.errors.ConfigError` (result code 3).

``lawkeep.toml`` is fingerprinted in ``laws.lock`` like a law file, because changing it
(for example fewer examples per law) can weaken every law.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from lawkeep.errors import ConfigError
from lawkeep.paths import CONFIG_FILENAME

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

PERMISSION_KEYS = ("add_law", "add_example", "rename_target", "change_law", "weaken_law")
PERMISSION_VALUES = ("auto", "ask")
FIXED_ASK = ("weaken_law",)
BACKENDS = ("hypothesis", "crosshair")
AGENT_NAMES = ("claude-code", "codex", "cursor", "copilot", "other", "")

#: Non-interactive launch commands per agent. ``{prompt}`` is replaced by the prompt text as
#: one argument, ``{prompt_file}`` by a path to a file holding it; with neither, the prompt is
#: sent on stdin. Checked against each agent's public docs on 2026-11-21 (sources in
#: ``agent_launcher``'s docstring); no real agent has been started with them yet.
AGENT_PRESETS: dict[str, str] = {
    "claude-code": "claude -p --permission-mode acceptEdits {prompt}",
    "codex": "codex exec --sandbox workspace-write {prompt}",
    "cursor": "cursor-agent -p --force {prompt}",
    # No --allow-all-tools (it also allows any shell command): reads, plus writes only to
    # draft files and laws/strategies.py; shell and URL access denied (deny rules beat allow
    # rules). Sources and caveats: ``agent_launcher``'s docstring.
    "copilot": (
        "copilot -p {prompt} --no-ask-user"
        " --allow-tool 'read, write(laws/drafts/*), write(laws/strategies.py)'"
        " --deny-tool 'shell, url'"
    ),
}


@dataclass
class Permissions:
    add_law: str = "auto"
    add_example: str = "auto"
    rename_target: str = "auto"
    change_law: str = "ask"
    weaken_law: str = "ask"

    def get(self, key: str) -> str:
        if key not in PERMISSION_KEYS:
            raise KeyError(f"unknown permission {key!r}; known: {', '.join(PERMISSION_KEYS)}")
        return getattr(self, key)

    def to_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in PERMISSION_KEYS}


@dataclass
class Profile:
    name: str
    examples: int = 200
    derandomize: bool = True
    backend: str = "hypothesis"
    deadline_ms: int = 0  # 0 = no deadline
    seed: int | None = None  # only used when derandomize is false; None = random

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


DEFAULT_PROFILES = {
    "pr": Profile("pr", examples=200, derandomize=True),
    "deep": Profile("deep", examples=2000, derandomize=False),
}


@dataclass
class CheckConfig:
    profile: str = "pr"
    profiles: dict[str, Profile] = field(
        default_factory=lambda: {k: Profile(**v.__dict__) for k, v in DEFAULT_PROFILES.items()}
    )

    def get_profile(self, name: str | None = None) -> Profile:
        key = name or self.profile
        try:
            return self.profiles[key]
        except KeyError:
            raise ConfigError(
                f"There is no check profile called {key!r}.",
                hint=f"Known profiles: {', '.join(sorted(self.profiles))}.",
            ) from None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "profiles": {k: v.to_dict() for k, v in self.profiles.items()},
        }


@dataclass
class ProjectConfig:
    source: list[str] = field(default_factory=list[str])  # empty = auto ("src" or ".")

    def source_dirs(self, root: Path) -> list[Path]:
        """Folders holding the code under test, resolved against ``root``."""
        if self.source:
            return [(Path(root) / s).resolve() for s in self.source]
        src = Path(root) / "src"
        return [src.resolve()] if src.is_dir() else [Path(root).resolve()]

    def to_dict(self) -> dict[str, Any]:
        return {"source": list(self.source)}


@dataclass
class StrengthConfig:
    mutation_tool: str = "mutmut"
    quick_check: bool = True
    min_bugs_caught: int = 1

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AgentConfig:
    name: str = ""
    command: str = ""
    timeout_seconds: int = 900

    @property
    def is_set(self) -> bool:
        return bool(self.effective_command())

    def effective_command(self) -> str:
        """``command`` if set, else the preset for ``name`` (may be empty)."""
        return self.command or AGENT_PRESETS.get(self.name, "")

    def argv(
        self, prompt: str, prompt_file: str | os.PathLike[str] | None = None
    ) -> tuple[list[str], str | None]:
        """Build the argument list for launching the agent.

        Returns ``(argv, stdin_text)``. ``stdin_text`` is the prompt when the command has no
        ``{prompt}`` / ``{prompt_file}`` placeholder, else ``None``.
        Raises ConfigError when no command is configured.
        """
        template = self.effective_command()
        if not template:
            raise ConfigError(
                "No coding agent is set up for drafting laws.",
                hint='Add to lawkeep.toml, for example:\n[agent]\nname = "claude-code"',
            )
        parts = shlex.split(template)
        used = False
        out: list[str] = []
        for part in parts:
            if "{prompt}" in part:
                out.append(part.replace("{prompt}", prompt))
                used = True
            elif "{prompt_file}" in part:
                if prompt_file is None:
                    raise ConfigError(
                        "The agent command needs {prompt_file} but no file was given."
                    )
                out.append(part.replace("{prompt_file}", str(prompt_file)))
                used = True
            else:
                out.append(part)
        return out, (None if used else prompt)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Config:
    permissions: Permissions = field(default_factory=Permissions)
    check: CheckConfig = field(default_factory=CheckConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    strength: StrengthConfig = field(default_factory=StrengthConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    warnings: list[str] = field(default_factory=list[str])
    path: Path | None = None  # file it was loaded from; None = defaults only
    raw: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        return {
            "permissions": self.permissions.to_dict(),
            "check": self.check.to_dict(),
            "project": self.project.to_dict(),
            "strength": self.strength.to_dict(),
            "agent": self.agent.to_dict(),
            "warnings": list(self.warnings),
            "path": str(self.path) if self.path else None,
        }


def default_config() -> Config:
    return Config()


# ---------------------------------------------------------------------------------------------


def _unknown(where: str, key: str, known: tuple[str, ...] | list[str], warnings: list[str]) -> None:
    guess = difflib.get_close_matches(key, list(known), n=1)
    hint = f" Did you mean {guess[0]!r}?" if guess else ""
    warnings.append(f"Unknown setting {key!r} in {where} (ignored).{hint}")


def _expect(where: str, key: str, value: Any, typ: type | tuple[type, ...], what: str) -> Any:
    if isinstance(value, bool) and typ in (int, (int,)):
        raise ConfigError(f"{where} {key} should be {what}, not {value!r}.")
    if not isinstance(value, typ):
        raise ConfigError(f"{where} {key} should be {what}, not {value!r}.")
    return value


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] in {CONFIG_FILENAME} should be a table (a [{key}] section).")
    return cast("dict[str, Any]", value)


def _parse_profile(name: str, table: dict[str, Any], base: Profile, warnings: list[str]) -> Profile:
    where = f"[check.{name}]"
    p = dataclasses.replace(base, name=name)
    known = ("examples", "derandomize", "backend", "deadline_ms", "seed")
    for key, value in table.items():
        if key == "examples":
            p.examples = _expect(where, key, value, int, "a whole number like 200")
            if p.examples < 1:
                raise ConfigError(f"{where} examples should be at least 1 (got {p.examples}).")
        elif key == "derandomize":
            p.derandomize = _expect(where, key, value, bool, "true or false")
        elif key == "backend":
            _expect(where, key, value, str, '"hypothesis" or "crosshair"')
            if value not in BACKENDS:
                raise ConfigError(
                    f'{where} backend should be "hypothesis" or "crosshair", not {value!r}.'
                )
            p.backend = value
        elif key == "deadline_ms":
            p.deadline_ms = _expect(
                where, key, value, int, "a whole number of milliseconds (0 = none)"
            )
            if p.deadline_ms < 0:
                raise ConfigError(f"{where} deadline_ms cannot be negative.")
        elif key == "seed":
            p.seed = _expect(where, key, value, int, "a whole number")
        else:
            _unknown(where, key, known, warnings)
    return p


def parse_config(data: dict[str, Any], *, source: Path | None = None) -> Config:
    """Validate an already-parsed TOML document and return a :class:`Config`."""
    cfg = Config(path=source, raw=data)
    w = cfg.warnings
    known_sections = ("permissions", "check", "project", "strength", "agent")
    for key in data:
        if key not in known_sections:
            _unknown("lawkeep.toml", key, known_sections, w)

    # [permissions]
    for key, value in _table(data, "permissions").items():
        if key not in PERMISSION_KEYS:
            _unknown("[permissions]", key, PERMISSION_KEYS, w)
            continue
        if value not in PERMISSION_VALUES:
            raise ConfigError(
                f'[permissions] {key} should be "auto" or "ask", not {value!r}.',
                hint='"auto" lets the agent go ahead (you are told); "ask" waits for your yes.',
            )
        if key in FIXED_ASK and value != "ask":
            w.append(
                f'[permissions] {key} = "{value}" is not allowed: weakening a law always needs '
                f'a person\'s yes. lawkeep uses "ask".'
            )
            value = "ask"
        setattr(cfg.permissions, key, value)

    # [check]
    check = _table(data, "check")
    for key, value in check.items():
        if key == "profile":
            cfg.check.profile = _expect("[check]", key, value, str, 'a profile name like "pr"')
        elif isinstance(value, dict):
            base = cfg.check.profiles.get(key, Profile(key))
            cfg.check.profiles[key] = _parse_profile(key, cast("dict[str, Any]", value), base, w)
        else:
            _unknown("[check]", key, ["profile", *cfg.check.profiles], w)
    if cfg.check.profile not in cfg.check.profiles:
        raise ConfigError(
            f"[check] profile = {cfg.check.profile!r}, but there is no [check.{cfg.check.profile}] section.",
            hint=f"Known profiles: {', '.join(sorted(cfg.check.profiles))}.",
        )

    # [project]
    for key, value in _table(data, "project").items():
        if key == "source":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                raise ConfigError('[project] source should be a list of folders, like ["src"].')
            folders: list[str] = []
            for v in cast("list[object]", value):
                if not isinstance(v, str):
                    raise ConfigError('[project] source should be a list of folders, like ["src"].')
                folders.append(v)
            cfg.project.source = folders
        else:
            _unknown("[project]", key, ("source",), w)

    # [strength]
    for key, value in _table(data, "strength").items():
        if key == "mutation_tool":
            cfg.strength.mutation_tool = _expect(
                "[strength]", key, value, str, 'a tool name like "mutmut"'
            )
        elif key == "quick_check":
            cfg.strength.quick_check = _expect("[strength]", key, value, bool, "true or false")
        elif key == "min_bugs_caught":
            cfg.strength.min_bugs_caught = _expect("[strength]", key, value, int, "a whole number")
        else:
            _unknown("[strength]", key, ("mutation_tool", "quick_check", "min_bugs_caught"), w)

    # [agent]
    for key, value in _table(data, "agent").items():
        if key == "name":
            _expect("[agent]", key, value, str, "an agent name")
            if value not in AGENT_NAMES:
                w.append(
                    f"[agent] name {value!r} is not one lawkeep knows "
                    f"({', '.join(n for n in AGENT_NAMES if n)}); set [agent] command yourself."
                )
            cfg.agent.name = value
        elif key == "command":
            cfg.agent.command = _expect("[agent]", key, value, str, "a command line")
        elif key == "timeout_seconds":
            cfg.agent.timeout_seconds = _expect(
                "[agent]", key, value, int, "a whole number of seconds"
            )
        else:
            _unknown("[agent]", key, ("name", "command", "timeout_seconds"), w)
    return cfg


def loads(text: str, *, source: Path | None = None) -> Config:
    """Parse TOML text into a :class:`Config`."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        where = str(source) if source else CONFIG_FILENAME
        raise ConfigError(f"{where} is not valid TOML: {exc}") from None
    return parse_config(data, source=source)


def load_config(root: str | os.PathLike[str] | None) -> Config:
    """Load ``<root>/lawkeep.toml``. A missing file (or ``root=None``) gives the defaults."""
    if root is None:
        return default_config()
    path = Path(root) / CONFIG_FILENAME
    if not path.is_file():
        return default_config()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    return loads(text, source=path)


def render_default_toml(*, agent_name: str = "", source: list[str] | None = None) -> str:
    """The ``lawkeep.toml`` text written by ``lawkeep init``."""
    agent_line = (
        f'name = "{agent_name}"'
        if agent_name
        else 'name = ""  # "claude-code", "codex", "cursor" or "copilot"'
    )
    src = ", ".join(f'"{s}"' for s in (source or []))
    source_line = (
        f"source = [{src}]"
        if source
        else '# source = ["src"]   # default: "src" if it exists, else the project folder'
    )
    return f"""\
# lawkeep settings. See https://github.com/lawkeep/lawkeep
# This file is part of your laws: changing it outside lawkeep is flagged as an unrecorded law change.

[permissions]
# What a coding agent may do to your laws without asking you first.
# "auto" = go ahead and tell me; "ask" = wait for my yes.
add_law       = "auto"   # add a new law (stricter)
add_example   = "auto"   # add an example or a saved failing case (stricter)
rename_target = "auto"   # update a law after a rename, when lawkeep confirms the meaning is the same
change_law    = "ask"    # change what a law checks
weaken_law    = "ask"    # loosen, delete or skip a law, or try fewer cases (always "ask")

[check]
profile = "pr"

[check.pr]               # everyday runs and pull requests: the same code always gets the same verdict
examples    = 200
derandomize = true

[check.deep]             # nightly or on demand: a deeper random search
examples    = 2000
derandomize = false

[project]
{source_line}

[agent]                  # the coding agent `lawkeep draft` starts in a separate session
{agent_line}
# command = "..."        # override the launch command; {{prompt}} is replaced by the request
"""


DEFAULT_TOML = render_default_toml()
