"""Unit tests for the mng_claude_mind settings module."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mng_claude_mind.data_types import ClaudeMindSettings
from imbue.mng_claude_mind.settings import load_settings_from_host
from imbue.mng_claude_mind.settings import load_settings_from_path
from imbue.mng_llm.data_types import ChatModel
from imbue.mng_mind.conftest import StubCommandResult
from imbue.mng_mind.conftest import StubHost


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


# -- load_settings_from_host tests --


def test_load_settings_returns_defaults_when_file_missing() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    settings = load_settings_from_host(cast(Any, host), Path("/work"))
    assert settings == ClaudeMindSettings()


def test_load_settings_parses_toml_from_host() -> None:
    toml_content = '[chat]\nmodel = "claude-sonnet-4-6"\n'
    host = StubHost(text_file_contents={"minds.toml": toml_content})
    settings = load_settings_from_host(cast(Any, host), Path("/work"))
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")


def test_load_settings_returns_defaults_on_invalid_toml() -> None:
    host = StubHost(text_file_contents={"minds.toml": "not valid toml {{{"})
    settings = load_settings_from_host(cast(Any, host), Path("/work"))
    assert settings == ClaudeMindSettings()


def test_load_settings_returns_defaults_on_read_failure() -> None:
    # Host says file exists but read_text_file raises
    host = StubHost()
    settings = load_settings_from_host(cast(Any, host), Path("/work"))
    # File check passes (default success), but read_text_file raises FileNotFoundError
    # which is caught. Defaults returned.
    assert settings.chat.model is None


# -- load_settings_from_path tests --


def test_load_settings_from_path_returns_defaults_when_file_missing(tmp_path: Path) -> None:
    settings = load_settings_from_path(tmp_path / "nonexistent.toml")
    assert settings == ClaudeMindSettings()


def test_load_settings_from_path_parses_toml(tmp_path: Path) -> None:
    settings_file = tmp_path / "minds.toml"
    settings_file.write_text('[chat]\nmodel = "claude-sonnet-4-6"\n')
    settings = load_settings_from_path(settings_file)
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")


def test_load_settings_from_host_returns_defaults_on_validation_error() -> None:
    """Settings with invalid field types should return defaults."""
    toml_content = '[watchers]\nevent_poll_interval_seconds = "not_a_number"\n'
    host = StubHost(text_file_contents={"minds.toml": toml_content})
    settings = load_settings_from_host(cast(Any, host), Path("/work"))
    assert settings == ClaudeMindSettings()
