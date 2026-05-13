"""Unit tests for GeminiAgentConfig and GeminiAgent."""

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr_gemini.plugin import GeminiAgent
from imbue.mngr_gemini.plugin import GeminiAgentConfig
from imbue.mngr_gemini.plugin import register_agent_type


def test_gemini_agent_config_has_correct_defaults() -> None:
    """Verify that GeminiAgentConfig has the expected default values."""
    config = GeminiAgentConfig()

    assert str(config.command) == "gemini"
    assert config.cli_args == ()
    assert config.permissions == []
    assert config.parent_type is None


def test_gemini_agent_config_merge_with_override() -> None:
    """Verify that merge_with works correctly for GeminiAgentConfig."""
    base = GeminiAgentConfig()
    override = GeminiAgentConfig(cli_args=("--verbose",))

    merged = base.merge_with(override)

    assert isinstance(merged, GeminiAgentConfig)
    assert merged.cli_args == ("--verbose",)
    assert str(merged.command) == "gemini"


def test_gemini_agent_subclasses_interactive_tui_agent() -> None:
    assert issubclass(GeminiAgent, InteractiveTuiAgent)


def test_gemini_agent_advertises_tui_ready_indicator() -> None:
    assert "Type your message" in GeminiAgent.TUI_READY_INDICATOR


def test_register_agent_type_returns_gemini_class_and_config() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "gemini"
    assert agent_class is GeminiAgent
    assert config_class is GeminiAgentConfig
