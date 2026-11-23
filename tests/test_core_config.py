"""Tests for lawkeep.config (lawkeep.toml)."""

from __future__ import annotations

import pytest

from lawkeep import config
from lawkeep.errors import ConfigError, SetupError


def test_defaults():
    cfg = config.default_config()
    assert cfg.permissions.to_dict() == {
        "add_law": "auto",
        "add_example": "auto",
        "rename_target": "auto",
        "change_law": "ask",
        "weaken_law": "ask",
    }
    assert cfg.check.get_profile().name == "pr"
    assert cfg.check.get_profile("pr").derandomize is True
    assert cfg.check.get_profile("deep").examples > cfg.check.get_profile("pr").examples
    assert cfg.warnings == []


def test_default_toml_round_trips():
    cfg = config.loads(config.DEFAULT_TOML)
    assert cfg.warnings == []
    assert cfg.permissions.to_dict() == config.default_config().permissions.to_dict()
    assert cfg.check.to_dict() == config.default_config().check.to_dict()
    text = config.render_default_toml(agent_name="claude-code", source=["src", "lib"])
    cfg2 = config.loads(text)
    assert cfg2.agent.name == "claude-code"
    assert cfg2.project.source == ["src", "lib"]
    assert cfg2.warnings == []


def test_weaken_law_auto_is_forced_to_ask():
    cfg = config.loads('[permissions]\nweaken_law = "auto"\nchange_law = "auto"\n')
    assert cfg.permissions.weaken_law == "ask"
    assert cfg.permissions.change_law == "auto"
    assert any("weaken_law" in w and "always needs" in w for w in cfg.warnings)


def test_bad_permission_value():
    with pytest.raises(ConfigError, match='"auto" or "ask"'):
        config.loads('[permissions]\nadd_law = "yes"\n')


def test_unknown_keys_warn_with_suggestion():
    cfg = config.loads('[permissions]\nadd_lwa = "auto"\n[chek]\nx = 1\n')
    assert any("'add_law'" in w for w in cfg.warnings)
    assert any("'check'" in w for w in cfg.warnings)


def test_profiles():
    cfg = config.loads(
        '[check]\nprofile = "deep"\n[check.deep]\nexamples = 50\nbackend = "crosshair"\n'
        "[check.nightly]\nexamples = 9000\nderandomize = false\nseed = 7\n"
    )
    assert cfg.check.get_profile().name == "deep"
    assert cfg.check.get_profile().examples == 50
    assert cfg.check.get_profile().backend == "crosshair"
    assert cfg.check.get_profile().derandomize is False  # kept from the built-in deep default
    assert cfg.check.get_profile("nightly").seed == 7
    with pytest.raises(ConfigError, match="no check profile"):
        cfg.check.get_profile("nope")


@pytest.mark.parametrize(
    "text, msg",
    [
        ("[check.pr]\nexamples = 0\n", "at least 1"),
        ("[check.pr]\nexamples = true\n", "whole number"),
        ('[check.pr]\nbackend = "z3"\n', "crosshair"),
        ("[check.pr]\nderandomize = 1\n", "true or false"),
        ('[check]\nprofile = "x"\n', "no [check.x]"),
        ("permissions = 3\n", "should be a table"),
        ("[project]\nsource = 3\n", "list of folders"),
        ("not toml = = =\n", "not valid TOML"),
    ],
)
def test_bad_values(text, msg):
    with pytest.raises(ConfigError, match=msg.replace("[", r"\[").replace("]", r"\]")):
        config.loads(text)


def test_config_error_is_setup_error():
    assert issubclass(ConfigError, SetupError)
    assert ConfigError("x").exit_code == 3


def test_load_config_missing_file_gives_defaults(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg.path is None and cfg.permissions.weaken_law == "ask"
    (tmp_path / "lawkeep.toml").write_text('[permissions]\nadd_law = "ask"\n')
    cfg = config.load_config(tmp_path)
    assert cfg.permissions.add_law == "ask"
    assert cfg.path == tmp_path / "lawkeep.toml"


def test_project_source_dirs(tmp_path):
    cfg = config.default_config()
    assert cfg.project.source_dirs(tmp_path) == [tmp_path.resolve()]
    (tmp_path / "src").mkdir()
    assert cfg.project.source_dirs(tmp_path) == [(tmp_path / "src").resolve()]
    cfg = config.loads('[project]\nsource = "lib"\n')
    assert cfg.project.source_dirs(tmp_path) == [(tmp_path / "lib").resolve()]


def test_agent_argv():
    agent = config.AgentConfig(command="myagent run {prompt} --quiet")
    argv, stdin = agent.argv("draft laws for billing")
    assert argv == ["myagent", "run", "draft laws for billing", "--quiet"]
    assert stdin is None
    argv, stdin = config.AgentConfig(command="myagent --file {prompt_file}").argv("p", "/tmp/p.txt")
    assert argv == ["myagent", "--file", "/tmp/p.txt"] and stdin is None
    argv, stdin = config.AgentConfig(command="myagent -").argv("hello")
    assert argv == ["myagent", "-"] and stdin == "hello"
    preset = config.AgentConfig(name="claude-code")
    assert preset.is_set and preset.argv("x")[0][0] == "claude"
    with pytest.raises(ConfigError, match="No coding agent"):
        config.AgentConfig().argv("x")
    with pytest.raises(ConfigError, match="prompt_file"):
        config.AgentConfig(command="a {prompt_file}").argv("x")


def test_agent_section_validation():
    cfg = config.loads(
        '[agent]\nname = "zed"\ncommand = "zed-agent {prompt}"\ntimeout_seconds = 60\n'
    )
    assert cfg.agent.command == "zed-agent {prompt}" and cfg.agent.timeout_seconds == 60
    assert any("not one lawkeep knows" in w for w in cfg.warnings)
    with pytest.raises(ConfigError):
        config.loads('[agent]\ntimeout_seconds = "long"\n')


def test_to_dict_is_json_ready():
    import json

    json.dumps(config.default_config().to_dict())
