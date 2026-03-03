"""Unit tests for the mng_claude_zygote settings module."""

from pathlib import Path
from typing import Any
from typing import cast

from imbue.mng_claude_zygote.conftest import StubCommandResult
from imbue.mng_claude_zygote.conftest import StubHost
from imbue.mng_claude_zygote.data_types import ChatModel
from imbue.mng_claude_zygote.data_types import ChatSettings
from imbue.mng_claude_zygote.data_types import ClaudeZygoteSettings
from imbue.mng_claude_zygote.data_types import ContextSettings
from imbue.mng_claude_zygote.data_types import ExtraContextSettings
from imbue.mng_claude_zygote.data_types import ProvisioningSettings
from imbue.mng_claude_zygote.data_types import WatcherSettings
from imbue.mng_claude_zygote.settings import load_settings_from_host

# -- ClaudeZygoteSettings default tests --


def test_settings_default_chat_model_is_none() -> None:
    """Default chat model is None (chat.sh falls back to its hardcoded default)."""
    settings = ClaudeZygoteSettings()
    assert settings.chat.model is None


def test_settings_default_context_values() -> None:
    settings = ClaudeZygoteSettings()
    assert settings.chat.context.max_transcript_line_count == 10
    assert settings.chat.context.max_messages_line_count == 20
    assert settings.chat.context.max_messages_per_conversation == 3
    assert settings.chat.context.max_trigger_line_count == 5
    assert settings.chat.context.max_content_length == 200


def test_settings_default_extra_context_values() -> None:
    settings = ClaudeZygoteSettings()
    assert settings.chat.extra_context.max_content_length == 300
    assert settings.chat.extra_context.transcript_line_count == 50
    assert settings.chat.extra_context.mng_list_hard_timeout_seconds == 120.0
    assert settings.chat.extra_context.mng_list_warn_threshold_seconds == 15.0


def test_settings_default_watcher_values() -> None:
    settings = ClaudeZygoteSettings()
    assert settings.watchers.conversation_poll_interval_seconds == 5
    assert settings.watchers.event_poll_interval_seconds == 3
    assert settings.watchers.watched_event_sources == ("messages", "scheduled", "mng_agents", "stop")


def test_settings_default_provisioning_values() -> None:
    settings = ClaudeZygoteSettings()
    assert settings.provisioning.fs_hard_timeout_seconds == 16.0
    assert settings.provisioning.fs_warn_threshold_seconds == 4.0
    assert settings.provisioning.command_check_hard_timeout_seconds == 30.0
    assert settings.provisioning.command_check_warn_threshold_seconds == 5.0
    assert settings.provisioning.install_hard_timeout_seconds == 300.0
    assert settings.provisioning.install_warn_threshold_seconds == 60.0


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


# -- Section model tests --


def test_chat_settings_is_frozen() -> None:
    settings = ChatSettings()
    assert settings.model_config.get("frozen") is True


def test_context_settings_is_frozen() -> None:
    settings = ContextSettings()
    assert settings.model_config.get("frozen") is True


def test_extra_context_settings_is_frozen() -> None:
    settings = ExtraContextSettings()
    assert settings.model_config.get("frozen") is True


def test_watcher_settings_is_frozen() -> None:
    settings = WatcherSettings()
    assert settings.model_config.get("frozen") is True


def test_provisioning_settings_is_frozen() -> None:
    settings = ProvisioningSettings()
    assert settings.model_config.get("frozen") is True


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
