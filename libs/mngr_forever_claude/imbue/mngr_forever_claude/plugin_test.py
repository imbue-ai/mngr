"""Unit tests for the forever-claude plugin."""

from __future__ import annotations

from typing import Any

import pytest

from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_forever_claude.plugin import (
    BOOTSTRAP_COMMAND,
    BOOTSTRAP_WINDOW_NAME,
    ForeverClaudeAgent,
    ForeverClaudeConfig,
    TELEGRAM_COMMAND,
    TELEGRAM_WINDOW_NAME,
    _get_agent_type_from_params,
    _inject_extra_windows,
    _is_forever_claude_agent_type,
    _validate_telegram_env_vars,
    override_command_options,
    register_agent_type,
)


# --- ForeverClaudeConfig defaults ---


def test_config_default_trust_working_directory() -> None:
    config = ForeverClaudeConfig()
    assert config.trust_working_directory is True


def test_config_default_model() -> None:
    config = ForeverClaudeConfig()
    assert config.model == "opus[1m]"


def test_config_default_is_fast() -> None:
    config = ForeverClaudeConfig()
    assert config.is_fast is True


# --- Telegram env var validation ---


def _make_options(env_vars: dict[str, str]) -> CreateAgentOptions:
    return CreateAgentOptions.model_construct(
        environment=AgentEnvironmentOptions(
            env_vars=tuple(EnvVar(key=k, value=v) for k, v in env_vars.items()),
        ),
    )


def test_validate_telegram_env_vars_valid() -> None:
    options = _make_options({
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_USER_NAME": "testuser",
    })
    _validate_telegram_env_vars(options)


def test_validate_telegram_env_vars_missing_bot_token() -> None:
    options = _make_options({"TELEGRAM_USER_NAME": "testuser"})
    with pytest.raises(PluginMngrError, match="TELEGRAM_BOT_TOKEN"):
        _validate_telegram_env_vars(options)


def test_validate_telegram_env_vars_missing_user_name() -> None:
    options = _make_options({"TELEGRAM_BOT_TOKEN": "123:abc"})
    with pytest.raises(PluginMngrError, match="TELEGRAM_USER_NAME"):
        _validate_telegram_env_vars(options)


def test_validate_telegram_env_vars_missing_both() -> None:
    options = _make_options({})
    with pytest.raises(PluginMngrError, match="TELEGRAM_BOT_TOKEN.*TELEGRAM_USER_NAME"):
        _validate_telegram_env_vars(options)


# --- Extra window injection ---


def test_inject_extra_windows_adds_both() -> None:
    params: dict[str, Any] = {}
    _inject_extra_windows(params)
    windows = params["extra_window"]
    assert len(windows) == 2
    assert f'{BOOTSTRAP_WINDOW_NAME}="{BOOTSTRAP_COMMAND}"' in windows
    assert f'{TELEGRAM_WINDOW_NAME}="{TELEGRAM_COMMAND}"' in windows


def test_inject_extra_windows_preserves_existing() -> None:
    params: dict[str, Any] = {"extra_window": ('existing="echo hello"',)}
    _inject_extra_windows(params)
    windows = params["extra_window"]
    assert len(windows) == 3
    assert windows[0] == 'existing="echo hello"'


# --- Agent type registration ---


def test_register_agent_type_returns_correct_tuple() -> None:
    name, agent_class, config_class = register_agent_type()
    assert name == "forever-claude"
    assert agent_class is ForeverClaudeAgent
    assert config_class is ForeverClaudeConfig


def test_register_agent_type_is_claude_subclass() -> None:
    _, agent_class, _ = register_agent_type()
    assert issubclass(agent_class, ClaudeAgent)


# --- Agent type param extraction ---


def test_get_agent_type_from_type_key() -> None:
    assert _get_agent_type_from_params({"type": "forever-claude"}) == "forever-claude"


def test_get_agent_type_from_positional_key() -> None:
    assert _get_agent_type_from_params({"positional_agent_type": "forever-claude"}) == "forever-claude"


def test_get_agent_type_type_takes_precedence() -> None:
    params = {"type": "forever-claude", "positional_agent_type": "other"}
    assert _get_agent_type_from_params(params) == "forever-claude"


def test_get_agent_type_returns_none_when_missing() -> None:
    assert _get_agent_type_from_params({}) is None


# --- Agent type detection ---


def test_is_forever_claude_agent_type_recognized() -> None:
    register_agent_class("forever-claude", ForeverClaudeAgent)
    assert _is_forever_claude_agent_type("forever-claude") is True


def test_is_forever_claude_agent_type_unknown_returns_false() -> None:
    assert _is_forever_claude_agent_type("nonexistent-type-xyz") is False


# --- override_command_options ---


def test_override_skips_non_create_commands() -> None:
    params: dict[str, Any] = {"type": "forever-claude"}
    override_command_options(command_name="list", command_class=object, params=params)
    assert "extra_window" not in params


def test_override_skips_when_no_agent_type() -> None:
    params: dict[str, Any] = {}
    override_command_options(command_name="create", command_class=object, params=params)
    assert "extra_window" not in params


def test_override_skips_non_forever_claude_type() -> None:
    params: dict[str, Any] = {"type": "claude"}
    override_command_options(command_name="create", command_class=object, params=params)
    assert "extra_window" not in params


def test_override_injects_windows_for_forever_claude() -> None:
    register_agent_class("forever-claude", ForeverClaudeAgent)
    params: dict[str, Any] = {"type": "forever-claude"}
    override_command_options(command_name="create", command_class=object, params=params)
    assert "extra_window" in params
    assert len(params["extra_window"]) == 2
