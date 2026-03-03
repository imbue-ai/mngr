"""Unit tests for the mng_claude_zygote settings module."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mng_claude_zygote.conftest import StubCommandResult
from imbue.mng_claude_zygote.conftest import StubHost
from imbue.mng_claude_zygote.data_types import ChatModel
from imbue.mng_claude_zygote.data_types import ClaudeZygoteSettings
from imbue.mng_claude_zygote.settings import load_settings_from_host


def test_settings_from_partial_toml() -> None:
    """Verify that partial TOML data fills in defaults for missing sections."""
    settings = ClaudeZygoteSettings.model_validate({"chat": {"model": "claude-sonnet-4-6"}})
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
    settings = ClaudeZygoteSettings.model_validate(data)
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")
    assert settings.chat.context.max_content_length == 500
    assert settings.chat.extra_context.transcript_line_count == 100
    assert settings.watchers.event_poll_interval_seconds == 10
    assert settings.provisioning.fs_hard_timeout_seconds == 30.0


# -- load_settings_from_host tests --


def test_load_settings_returns_defaults_when_file_missing() -> None:
    host = StubHost(command_results={"test -f": StubCommandResult(success=False)})
    settings = load_settings_from_host(cast(Any, host), Path("/work"), ".changelings")
    assert settings == ClaudeZygoteSettings()


def test_load_settings_parses_toml_from_host() -> None:
    toml_content = '[chat]\nmodel = "claude-sonnet-4-6"\n'
    host = StubHost(text_file_contents={"settings.toml": toml_content})
    settings = load_settings_from_host(cast(Any, host), Path("/work"), ".changelings")
    assert settings.chat.model == ChatModel("claude-sonnet-4-6")


def test_load_settings_returns_defaults_on_invalid_toml() -> None:
    host = StubHost(text_file_contents={"settings.toml": "not valid toml {{{"})
    settings = load_settings_from_host(cast(Any, host), Path("/work"), ".changelings")
    assert settings == ClaudeZygoteSettings()


def test_load_settings_returns_defaults_on_read_failure() -> None:
    # Host says file exists but read_text_file raises
    host = StubHost()
    settings = load_settings_from_host(cast(Any, host), Path("/work"), ".changelings")
    # File check passes (default success), but read_text_file raises FileNotFoundError
    # which is caught. Defaults returned.
    assert settings.chat.model is None
