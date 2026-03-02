"""Unit tests for the mng_claude_zygote plugin."""

from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng_claude_zygote.plugin import AGENT_TTYD_COMMAND
from imbue.mng_claude_zygote.plugin import AGENT_TTYD_SERVER_NAME
from imbue.mng_claude_zygote.plugin import AGENT_TTYD_WINDOW_NAME
from imbue.mng_claude_zygote.plugin import CHAT_TTYD_COMMAND
from imbue.mng_claude_zygote.plugin import CHAT_TTYD_SERVER_NAME
from imbue.mng_claude_zygote.plugin import CHAT_TTYD_WINDOW_NAME
from imbue.mng_claude_zygote.plugin import CONV_WATCHER_COMMAND
from imbue.mng_claude_zygote.plugin import CONV_WATCHER_WINDOW_NAME
from imbue.mng_claude_zygote.plugin import ClaudeZygoteAgent
from imbue.mng_claude_zygote.plugin import ClaudeZygoteConfig
from imbue.mng_claude_zygote.plugin import EVENT_WATCHER_COMMAND
from imbue.mng_claude_zygote.plugin import EVENT_WATCHER_WINDOW_NAME
from imbue.mng_claude_zygote.plugin import get_agent_type_from_params
from imbue.mng_claude_zygote.plugin import inject_agent_ttyd
from imbue.mng_claude_zygote.plugin import inject_changeling_windows
from imbue.mng_claude_zygote.plugin import override_command_options
from imbue.mng_claude_zygote.plugin import register_agent_type

# Total number of tmux windows injected by inject_changeling_windows:
# agent ttyd, conv_watcher, events, chat ttyd
_CHANGELING_WINDOW_COUNT = 4


class _DummyCommandClass:
    pass


@pytest.fixture()
def zygote_create_params() -> dict[str, Any]:
    """Run override_command_options for a claude-zygote create and return the modified params."""
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude-zygote"}
    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )
    return params


# -- Class hierarchy tests --


def test_claude_zygote_agent_inherits_from_claude_agent() -> None:
    assert issubclass(ClaudeZygoteAgent, ClaudeAgent)


# -- Config tests --


def test_claude_zygote_config_defaults_trust_to_true() -> None:
    config = ClaudeZygoteConfig()
    assert config.trust_working_directory is True


def test_claude_zygote_config_inherits_from_claude_agent_config() -> None:
    assert issubclass(ClaudeZygoteConfig, ClaudeAgentConfig)


def test_claude_zygote_config_overrides_base_trust_default() -> None:
    base = ClaudeAgentConfig()
    zygote = ClaudeZygoteConfig()
    assert base.trust_working_directory is False
    assert zygote.trust_working_directory is True


def test_claude_zygote_config_has_install_llm_default() -> None:
    config = ClaudeZygoteConfig()
    assert config.install_llm is True


def test_claude_zygote_config_has_changelings_dir_name() -> None:
    config = ClaudeZygoteConfig()
    assert config.changelings_dir_name == ".changelings"


# -- override_command_options hook tests --


def test_adds_all_changeling_windows(zygote_create_params: dict[str, Any]) -> None:
    """Verify that the plugin adds all 4 changeling windows."""
    assert len(zygote_create_params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_adds_agent_ttyd(zygote_create_params: dict[str, Any]) -> None:
    assert AGENT_TTYD_WINDOW_NAME in zygote_create_params["add_command"][0]
    assert AGENT_TTYD_COMMAND in zygote_create_params["add_command"][0]


def test_adds_conv_watcher_window(zygote_create_params: dict[str, Any]) -> None:
    entries = [c for c in zygote_create_params["add_command"] if CONV_WATCHER_WINDOW_NAME in c]
    assert len(entries) == 1
    assert CONV_WATCHER_COMMAND in entries[0]


def test_adds_event_watcher_window(zygote_create_params: dict[str, Any]) -> None:
    entries = [c for c in zygote_create_params["add_command"] if EVENT_WATCHER_WINDOW_NAME in c]
    assert len(entries) == 1
    assert EVENT_WATCHER_COMMAND in entries[0]


def test_adds_chat_ttyd_window(zygote_create_params: dict[str, Any]) -> None:
    entries = [c for c in zygote_create_params["add_command"] if CHAT_TTYD_WINDOW_NAME in c]
    assert len(entries) == 1


def test_adds_changeling_windows_for_positional_agent_type() -> None:
    params: dict[str, Any] = {"add_command": (), "positional_agent_type": "claude-zygote"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_does_not_modify_non_create_commands() -> None:
    params: dict[str, Any] = {"add_command": (), "agent_type": "claude-zygote"}
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


def test_preserves_existing_add_commands() -> None:
    params: dict[str, Any] = {"add_command": ('monitor="htop"',), "agent_type": "claude-zygote"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT + 1
    assert params["add_command"][0] == 'monitor="htop"'


# -- inject_agent_ttyd tests (direct function) --


def test_inject_agent_ttyd_adds_command() -> None:
    params: dict[str, Any] = {}
    inject_agent_ttyd(params)
    assert len(params["add_command"]) == 1
    assert AGENT_TTYD_WINDOW_NAME in params["add_command"][0]


def test_inject_agent_ttyd_preserves_existing() -> None:
    params: dict[str, Any] = {"add_command": ('foo="bar"',)}
    inject_agent_ttyd(params)
    assert len(params["add_command"]) == 2


# -- inject_changeling_windows tests --


def test_inject_changeling_windows_adds_all_windows() -> None:
    """Verify that inject_changeling_windows adds all 4 windows."""
    params: dict[str, Any] = {}
    inject_changeling_windows(params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT


def test_inject_changeling_windows_preserves_existing() -> None:
    params: dict[str, Any] = {"add_command": ('foo="bar"',)}
    inject_changeling_windows(params)
    assert len(params["add_command"]) == _CHANGELING_WINDOW_COUNT + 1
    assert params["add_command"][0] == 'foo="bar"'


# -- Agent ttyd command content tests --


def test_agent_ttyd_command_is_parseable_as_named_command() -> None:
    params: dict[str, Any] = {}
    inject_agent_ttyd(params)
    named_cmd = NamedCommand.from_string(params["add_command"][0])
    assert named_cmd.window_name == AGENT_TTYD_WINDOW_NAME
    assert str(named_cmd.command) == AGENT_TTYD_COMMAND


def test_agent_ttyd_command_uses_random_port() -> None:
    assert "ttyd -p 0" in AGENT_TTYD_COMMAND


def test_agent_ttyd_command_writes_server_log() -> None:
    assert "servers.jsonl" in AGENT_TTYD_COMMAND
    assert AGENT_TTYD_SERVER_NAME in AGENT_TTYD_COMMAND
    assert "MNG_AGENT_STATE_DIR" in AGENT_TTYD_COMMAND


def test_agent_ttyd_command_watches_stderr_for_port() -> None:
    assert "Listening on port:" in AGENT_TTYD_COMMAND


def test_agent_ttyd_command_attaches_to_session() -> None:
    assert "tmux attach" in AGENT_TTYD_COMMAND
    assert "session_name" in AGENT_TTYD_COMMAND


def test_agent_ttyd_command_unsets_tmux_env() -> None:
    assert "unset TMUX" in AGENT_TTYD_COMMAND


def test_agent_ttyd_command_skips_log_when_no_state_dir() -> None:
    assert 'if [ -n "$MNG_AGENT_STATE_DIR" ]' in AGENT_TTYD_COMMAND


# -- Chat ttyd command content tests --


def test_chat_ttyd_command_uses_random_port() -> None:
    assert "ttyd -p 0" in CHAT_TTYD_COMMAND


def test_chat_ttyd_command_writes_server_log() -> None:
    assert "servers.jsonl" in CHAT_TTYD_COMMAND
    assert CHAT_TTYD_SERVER_NAME in CHAT_TTYD_COMMAND


def test_chat_ttyd_command_runs_chat_script() -> None:
    assert "chat.sh" in CHAT_TTYD_COMMAND


# -- Watcher command content tests --


def test_conv_watcher_command_references_script() -> None:
    assert "conversation_watcher.sh" in CONV_WATCHER_COMMAND


def test_event_watcher_command_references_script() -> None:
    assert "event_watcher.sh" in EVENT_WATCHER_COMMAND


# -- ClaudeZygoteAgent._get_zygote_config tests --


def test_get_zygote_config_raises_on_wrong_type() -> None:
    """Verify that _get_zygote_config raises RuntimeError for non-ClaudeZygoteConfig."""
    agent_stub = SimpleNamespace(agent_config=ClaudeAgentConfig())

    with pytest.raises(RuntimeError, match="ClaudeZygoteAgent requires ClaudeZygoteConfig"):
        ClaudeZygoteAgent._get_zygote_config(cast(Any, agent_stub))


def test_get_zygote_config_returns_config_when_correct_type() -> None:
    """Verify that _get_zygote_config returns the config when it is the correct type."""
    config = ClaudeZygoteConfig()
    agent_stub = SimpleNamespace(agent_config=config)

    result = ClaudeZygoteAgent._get_zygote_config(cast(Any, agent_stub))
    assert result is config


# -- register_agent_type hook tests --


def test_register_agent_type_returns_correct_name() -> None:
    name, agent_cls, config_cls = register_agent_type()
    assert name == "claude-zygote"


def test_register_agent_type_returns_correct_agent_class() -> None:
    _, agent_cls, _ = register_agent_type()
    assert agent_cls is ClaudeZygoteAgent


def test_register_agent_type_returns_correct_config_class() -> None:
    _, _, config_cls = register_agent_type()
    assert config_cls is ClaudeZygoteConfig


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


# -- Config customization tests --


def test_claude_zygote_config_allows_disabling_llm_install() -> None:
    config = ClaudeZygoteConfig(install_llm=False)
    assert config.install_llm is False


def test_claude_zygote_config_allows_custom_changelings_dir() -> None:
    config = ClaudeZygoteConfig(changelings_dir_name=".custom_dir")
    assert config.changelings_dir_name == ".custom_dir"


def test_claude_zygote_config_allows_disabling_trust() -> None:
    config = ClaudeZygoteConfig(trust_working_directory=False)
    assert config.trust_working_directory is False


# -- Chat ttyd additional tests --


def test_chat_ttyd_command_is_parseable_as_named_command() -> None:
    """Verify the chat ttyd command is parseable as a NamedCommand."""
    params: dict[str, Any] = {}
    inject_changeling_windows(params)
    chat_entries = [c for c in params["add_command"] if CHAT_TTYD_WINDOW_NAME in c]
    assert len(chat_entries) == 1
    named_cmd = NamedCommand.from_string(chat_entries[0])
    assert named_cmd.window_name == CHAT_TTYD_WINDOW_NAME
