"""Unit tests for claude mind settings loading via the agent class."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mngr_claude_mind.data_types import ClaudeMindSettings
from imbue.mngr_claude_mind.plugin import ClaudeMindAgent
from imbue.mngr_claude_mind.plugin import ClaudeMindConfig
from imbue.mngr_llm.data_types import ChatModel
from imbue.mngr_llm.data_types import LlmSettings
from imbue.mngr_llm.settings import load_from_path
from imbue.mngr_mind.conftest import StubCommandResult
from imbue.mngr_mind.conftest import StubHost


def test_settings_from_partial_toml() -> None:
    """Verify that partial TOML data fills in defaults for missing sections."""
    settings = ClaudeMindSettings.model_validate({"chat": {"model": "claude-sonnet-4-6"}})
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")
    # Other sections should have defaults
    assert settings.chat.context.max_transcript_line_count == 10
    assert settings.watchers.event_poll_interval_seconds == 3


def test_settings_from_full_toml() -> None:
    """Verify that all sections can be overridden."""
    data = {
        "chat": {
            "model": "claude-sonnet-4-6",
            "context": {"max_content_length": 500},
            "extra_context": {"transcript_line_count": 100},
        },
        "watchers": {"event_poll_interval_seconds": 10},
        "provisioning": {"fs_hard_timeout_seconds": 30.0},
    }
    settings = ClaudeMindSettings.model_validate(data)
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")
    assert settings.chat.context.max_content_length == 500
    assert settings.chat.extra_context.transcript_line_count == 100
    assert settings.watchers.event_poll_interval_seconds == 10
    assert settings.provisioning.fs_hard_timeout_seconds == 30.0


# -- Agent class load_settings_from_host tests --


def _make_agent() -> ClaudeMindAgent:
    return ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
        work_dir=Path("/work"),
    )


def test_load_settings_returns_defaults_when_file_missing() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    assert settings == ClaudeMindSettings()


def test_load_settings_parses_toml_from_host() -> None:
    toml_content = '[chat]\nmodel = "claude-sonnet-4-6"\n'
    host = StubHost(text_file_contents={"minds.toml": toml_content})
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")


def test_load_settings_returns_defaults_on_invalid_toml() -> None:
    host = StubHost(text_file_contents={"minds.toml": "not valid toml {{{"})
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    assert settings == ClaudeMindSettings()


def test_load_settings_returns_defaults_on_read_failure() -> None:
    # Host says file exists but read_text_file raises
    host = StubHost()
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    # File check passes (default success), but read_text_file raises FileNotFoundError
    # which is caught. Defaults returned.
    assert settings.chat.model is None


# -- load_from_path with ClaudeMindSettings tests --


def test_load_from_path_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    settings = load_from_path(tmp_path / "nonexistent.toml", ClaudeMindSettings)
    assert settings == ClaudeMindSettings()


def test_load_from_path_parses_toml(tmp_path: Path) -> None:
    settings_file = tmp_path / "minds.toml"
    settings_file.write_text('[chat]\nmodel = "claude-sonnet-4-6"\n')
    settings = load_from_path(settings_file, ClaudeMindSettings)
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")


def test_load_settings_from_host_returns_defaults_on_validation_error() -> None:
    """Settings with invalid field types should return defaults."""
    toml_content = '[watchers]\nevent_poll_interval_seconds = "not_a_number"\n'
    host = StubHost(text_file_contents={"minds.toml": toml_content})
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    assert settings == ClaudeMindSettings()


# -- Verify correct settings class is used --


def test_agent_uses_claude_mind_settings_class() -> None:
    """ClaudeMindAgent._settings_class should be ClaudeMindSettings, not LlmSettings."""
    assert ClaudeMindAgent._settings_class is ClaudeMindSettings
    assert ClaudeMindAgent._settings_class is not LlmSettings


def test_agent_loads_claude_mind_specific_fields_from_host() -> None:
    """Verify that ClaudeMindAgent correctly loads fields only defined in ClaudeMindSettings."""
    toml_content = (
        'agent_type = "elena-code"\n\n'
        '[chat]\nmodel = "claude-sonnet-4-6"\n\n'
        "[watchers]\nevent_poll_interval_seconds = 7\n"
    )
    host = StubHost(text_file_contents={"minds.toml": toml_content})
    agent = _make_agent()
    settings = agent.load_settings_from_host(cast(Any, host))
    assert isinstance(settings, ClaudeMindSettings)
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")
    assert settings.agent_type == "elena-code"
    assert settings.watchers.event_poll_interval_seconds == 7
