"""Unit tests for the mng_claude_changeling plugin."""

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng_claude_changeling.plugin import CHAT_TTYD_WINDOW_NAME
from imbue.mng_claude_changeling.plugin import CONV_WATCHER_COMMAND
from imbue.mng_claude_changeling.plugin import CONV_WATCHER_WINDOW_NAME
from imbue.mng_claude_changeling.plugin import ClaudeChangelingAgent
from imbue.mng_claude_changeling.plugin import ClaudeChangelingConfig
from imbue.mng_claude_changeling.plugin import EVENT_WATCHER_COMMAND
from imbue.mng_claude_changeling.plugin import EVENT_WATCHER_WINDOW_NAME
from imbue.mng_claude_changeling.plugin import WEB_SERVER_WINDOW_NAME
from imbue.mng_claude_changeling.plugin import get_agent_type_from_params
from imbue.mng_claude_changeling.plugin import inject_changeling_windows
from imbue.mng_claude_changeling.plugin import override_command_options

# Total number of tmux windows injected by inject_changeling_windows:
# agent ttyd, conv_watcher, events, web_server, transcript, chat ttyd
_CHANGELING_WINDOW_COUNT = 6


class _DummyCommandClass:
    pass


@pytest.fixture()
def changeling_create_params() -> dict[str, Any]:
    """Run override_command_options for a claude-changeling create and return the modified params."""
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude-changeling"}
    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )
    return params


# -- override_command_options hook tests --


def test_adds_all_changeling_windows(changeling_create_params: dict[str, Any]) -> None:
    """Verify that the plugin adds all 5 changeling windows."""
    assert len(changeling_create_params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_adds_conv_watcher_window(changeling_create_params: dict[str, Any]) -> None:
    entries = [c for c in changeling_create_params["add_command"] if CONV_WATCHER_WINDOW_NAME in c]
    assert len(entries) == 1
    assert CONV_WATCHER_COMMAND in entries[0]


def test_adds_event_watcher_window(changeling_create_params: dict[str, Any]) -> None:
    prefix = f'{EVENT_WATCHER_WINDOW_NAME}="'
    entries = [c for c in changeling_create_params["add_command"] if c.startswith(prefix)]
    assert len(entries) == 1
    assert EVENT_WATCHER_COMMAND in entries[0]


def test_adds_web_server_window(changeling_create_params: dict[str, Any]) -> None:
    entries = [c for c in changeling_create_params["add_command"] if WEB_SERVER_WINDOW_NAME in c]
    assert len(entries) == 1


def test_adds_changeling_windows_for_positional_agent_type() -> None:
    params: dict[str, Any] = {"add_command": (), "positional_agent_type": "claude-changeling"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_does_not_modify_non_create_commands() -> None:
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude-changeling"}
    override_command_options(command_name="connect", command_class=_DummyCommandClass, params=params)
    assert params["add_command"] == ()


def test_does_not_modify_for_other_agent_types() -> None:
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert params["add_command"] == ()


def test_does_not_modify_when_no_agent_type() -> None:
    params: dict[str, Any] = {"add_command": ()}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert params["add_command"] == ()


def test_injects_windows_for_registered_subclass() -> None:
    """Verify that a registered agent type that subclasses ClaudeChangelingAgent gets changeling windows."""
    from imbue.mng.config.agent_class_registry import register_agent_class
    from imbue.mng.config.agent_class_registry import reset_agent_class_registry

    class _TestSubclassAgent(ClaudeChangelingAgent):
        """Test subclass for verifying subclass detection."""

    try:
        register_agent_class("test-subclass-82741", _TestSubclassAgent)
        params: dict[str, Any] = {"add_command": (), "agent_type": "test-subclass-82741"}
        override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
        assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT
    finally:
        reset_agent_class_registry()


def test_preserves_existing_add_commands() -> None:
    params: dict[str, Any] = {"add_command": ('monitor="htop"',), "agent_type": "claude-changeling"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT + 1
    assert params["add_command"][0] == 'monitor="htop"'


# -- inject_changeling_windows tests --


def test_inject_changeling_windows_adds_all_windows() -> None:
    """Verify that inject_changeling_windows adds all expected windows."""
    params: dict[str, Any] = {}
    inject_changeling_windows(params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_inject_changeling_windows_preserves_existing() -> None:
    params: dict[str, Any] = {"add_command": ('foo="bar"',)}
    inject_changeling_windows(params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT + 1
    assert params["add_command"][0] == 'foo="bar"'


# -- ClaudeChangelingAgent._get_changeling_config tests --


def test_get_changeling_config_raises_on_wrong_type() -> None:
    """Verify that _get_changeling_config raises RuntimeError for non-ClaudeChangelingConfig."""
    agent_stub = SimpleNamespace(agent_config=ClaudeAgentConfig())

    with pytest.raises(RuntimeError, match="ClaudeChangelingAgent requires ClaudeChangelingConfig"):
        ClaudeChangelingAgent._get_changeling_config(cast(Any, agent_stub))


def test_get_changeling_config_returns_config_when_correct_type() -> None:
    """Verify that _get_changeling_config returns the config when it is the correct type."""
    config = ClaudeChangelingConfig()
    agent_stub = SimpleNamespace(agent_config=config)

    result = ClaudeChangelingAgent._get_changeling_config(cast(Any, agent_stub))
    assert result is config


# -- get_agent_type_from_params tests --


def test_get_agent_type_from_params_returns_agent_type() -> None:
    assert get_agent_type_from_params({"agent_type": "claude"}) == "claude"


def test_get_agent_type_from_params_returns_positional() -> None:
    assert get_agent_type_from_params({"positional_agent_type": "codex"}) == "codex"


def test_get_agent_type_from_params_prefers_agent_type() -> None:
    params = {"agent_type": "claude", "positional_agent_type": "codex"}
    assert get_agent_type_from_params(params) == "claude"


def test_get_agent_type_from_params_returns_none_when_absent() -> None:
    assert get_agent_type_from_params({}) is None


# -- Web server additional tests --


def test_web_server_command_is_parseable_as_named_command() -> None:
    """Verify the web server command is parseable as a NamedCommand."""
    params: dict[str, Any] = {}
    inject_changeling_windows(params)
    web_entries = [c for c in params["add_command"] if WEB_SERVER_WINDOW_NAME in c]
    assert len(web_entries) == 1
    named_cmd = NamedCommand.from_string(web_entries[0])
    assert named_cmd.window_name == WEB_SERVER_WINDOW_NAME


# -- Chat ttyd tests --


def test_adds_chat_ttyd_window(changeling_create_params: dict[str, Any]) -> None:
    entries = [c for c in changeling_create_params["add_command"] if CHAT_TTYD_WINDOW_NAME in c]
    assert len(entries) == 1
