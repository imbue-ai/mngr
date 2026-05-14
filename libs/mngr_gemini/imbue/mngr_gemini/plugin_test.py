"""Unit tests for GeminiAgentConfig and GeminiAgent."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_gemini.plugin import GeminiAgent
from imbue.mngr_gemini.plugin import GeminiAgentConfig
from imbue.mngr_gemini.plugin import register_agent_type


def test_gemini_agent_config_has_correct_defaults() -> None:
    """Verify that GeminiAgentConfig has the expected default values."""
    config = GeminiAgentConfig()

    assert str(config.command) == "gemini"
    assert config.cli_args == ("--skip-trust",)
    assert config.permissions == []
    assert config.parent_type is None


def test_gemini_agent_config_merge_with_concatenates_skip_trust_and_user_args() -> None:
    """User-supplied cli_args concatenate after the default --skip-trust."""
    base = GeminiAgentConfig()
    override = GeminiAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, GeminiAgentConfig)
    assert merged.cli_args == ("--skip-trust", "--verbose")
    assert str(merged.command) == "gemini"


def test_gemini_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(GeminiAgent, InteractiveTuiAgent)


def test_gemini_agent_advertises_tui_ready_indicator() -> None:
    """Ready indicator is the stable header banner, not the dynamic placeholder."""
    assert GeminiAgent.TUI_READY_INDICATOR == "Gemini CLI"


def test_gemini_agent_advertises_input_cleared_indicator() -> None:
    """Input-cleared indicator is the dynamic placeholder used for post-Enter confirmation."""
    assert GeminiAgent.TUI_INPUT_CLEARED_INDICATOR == "Type your message"


def test_gemini_agent_disables_submission_signal() -> None:
    """Gemini lacks a UserPromptSubmit-style hook, so this hook must be off."""
    agent = GeminiAgent.model_construct()
    assert agent.uses_submission_signal() is False


def test_register_agent_type_returns_gemini_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "gemini"
    assert agent_class is GeminiAgent
    assert config_class is GeminiAgentConfig


@pytest.fixture
def gemini_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> GeminiAgent:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return GeminiAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-gemini"),
        agent_type=AgentTypeName("gemini"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=GeminiAgentConfig(),
        host=host,
    )


def test_assemble_command_includes_skip_trust_from_default_cli_args(gemini_agent: GeminiAgent) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, (), command_override=None)
    assert str(command).split() == ["gemini", "--skip-trust"]


def test_assemble_command_appends_user_agent_args_after_cli_args(gemini_agent: GeminiAgent) -> None:
    command = gemini_agent.assemble_command(gemini_agent.host, ("--debug",), command_override=None)
    assert str(command).split() == ["gemini", "--skip-trust", "--debug"]
