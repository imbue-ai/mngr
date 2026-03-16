"""Unit tests for the mng_llm plugin module."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.primitives import CommandString
from imbue.mng_llm.plugin import LlmAgent
from imbue.mng_llm.plugin import LlmAgentConfig
from imbue.mng_llm.plugin import register_agent_type
from imbue.mng_llm.plugin import set_llm_model_env_var
from imbue.mng_llm.plugin import set_uv_tool_env_vars

# -- set_uv_tool_env_vars tests --


def test_set_uv_tool_env_vars_sets_paths() -> None:
    env: dict[str, str] = {"MNG_AGENT_STATE_DIR": "/tmp/state"}
    set_uv_tool_env_vars(env)
    assert env["UV_TOOL_DIR"] == "/tmp/state/tools"
    assert env["UV_TOOL_BIN_DIR"] == "/tmp/state/bin"


def test_set_uv_tool_env_vars_no_op_without_state_dir() -> None:
    env: dict[str, str] = {}
    set_uv_tool_env_vars(env)
    assert "UV_TOOL_DIR" not in env
    assert "UV_TOOL_BIN_DIR" not in env


# -- set_llm_model_env_var tests --


def _make_host_stub(settings_file_exists: bool = False, settings_content: str = "") -> Any:
    """Create a host stub for set_llm_model_env_var tests."""

    def _execute_command(cmd: str, **kwargs: Any) -> Any:
        if "test -f" in cmd and settings_file_exists:
            return SimpleNamespace(success=True, stdout="", stderr="")
        return SimpleNamespace(success=False, stdout="", stderr="")

    def _read_text_file(path: Path) -> str:
        return settings_content

    return SimpleNamespace(
        execute_command=_execute_command,
        read_text_file=_read_text_file,
    )


def test_set_llm_model_env_var_uses_default_when_no_settings() -> None:
    host = _make_host_stub(settings_file_exists=False)
    env: dict[str, str] = {}
    set_llm_model_env_var(host, Path("/work"), env)
    assert env["MNG_LLM_MODEL"] == "claude-opus-4.6"


def test_set_llm_model_env_var_reads_model_from_settings() -> None:
    host = _make_host_stub(
        settings_file_exists=True,
        settings_content='[chat]\nmodel = "claude-haiku-4-5"\n',
    )
    env: dict[str, str] = {}
    set_llm_model_env_var(host, Path("/work"), env)
    assert env["MNG_LLM_MODEL"] == "claude-haiku-4-5"


def test_set_llm_model_env_var_uses_default_when_model_not_in_settings() -> None:
    host = _make_host_stub(
        settings_file_exists=True,
        settings_content="[chat]\n# no model key\n",
    )
    env: dict[str, str] = {}
    set_llm_model_env_var(host, Path("/work"), env)
    assert env["MNG_LLM_MODEL"] == "claude-opus-4.6"


# -- LlmAgentConfig tests --


def test_llm_agent_config_defaults() -> None:
    config = LlmAgentConfig()
    assert config.command == CommandString("llm")
    assert config.install_llm is True


def test_llm_agent_config_merge_with_overrides_command() -> None:
    base = LlmAgentConfig(command=CommandString("llm-custom"))
    override = LlmAgentConfig(command=CommandString("llm-override"))
    merged = base.merge_with(override)
    assert isinstance(merged, LlmAgentConfig)
    assert merged.command == CommandString("llm-override")


def test_llm_agent_config_merge_with_non_llm_returns_override() -> None:
    base = LlmAgentConfig()
    override = AgentTypeConfig()
    merged = base.merge_with(override)
    assert isinstance(merged, AgentTypeConfig)
    assert not isinstance(merged, LlmAgentConfig)


def test_llm_agent_config_merge_with_preserves_cli_args() -> None:
    base = LlmAgentConfig(cli_args=("--verbose",))
    override = LlmAgentConfig()
    merged = base.merge_with(override)
    assert isinstance(merged, LlmAgentConfig)
    assert merged.cli_args == ("--verbose",)


# -- register_agent_type tests --


def test_register_agent_type_returns_llm() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "llm"
    assert agent_class is LlmAgent
    assert config_class is LlmAgentConfig


# -- register_cli_commands tests --


def test_register_cli_commands_returns_commands() -> None:
    from imbue.mng_llm.plugin import register_cli_commands

    commands = register_cli_commands()
    assert commands is not None
    command_names = [c.name for c in commands]
    assert "llmconversations" in command_names
    assert "llmweb" in command_names
    assert "llmdb" in command_names
