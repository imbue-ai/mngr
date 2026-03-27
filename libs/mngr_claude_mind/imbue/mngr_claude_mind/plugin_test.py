"""Unit tests for the mngr_claude_mind plugin."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr.primitives import CommandString
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude_mind.plugin import CONV_WATCHER_COMMAND
from imbue.mngr_claude_mind.plugin import CONV_WATCHER_WINDOW_NAME
from imbue.mngr_claude_mind.plugin import ClaudeMindAgent
from imbue.mngr_claude_mind.plugin import ClaudeMindConfig
from imbue.mngr_claude_mind.plugin import EVENT_WATCHER_COMMAND
from imbue.mngr_claude_mind.plugin import EVENT_WATCHER_WINDOW_NAME
from imbue.mngr_claude_mind.plugin import OBSERVER_COMMAND
from imbue.mngr_claude_mind.plugin import OBSERVER_WINDOW_NAME
from imbue.mngr_claude_mind.plugin import WEB_SERVER_WINDOW_NAME
from imbue.mngr_claude_mind.plugin import get_agent_type_from_params
from imbue.mngr_claude_mind.plugin import inject_supporting_services
from imbue.mngr_claude_mind.plugin import override_command_options
from imbue.mngr_llm.data_types import ProvisioningSettings
from imbue.mngr_mind.conftest import StubHost

# Total number of tmux windows injected by inject_supporting_services:
# conv_watcher, events, web_server, observer
_SUPPORTING_SERVICE_COUNT = 4


class _DummyCommandClass:
    pass


@pytest.fixture()
def mind_create_params() -> dict[str, Any]:
    """Run override_command_options for a claude-mind create and return the modified params."""
    params: dict[str, Any] = {"extra_window": (), "type": "claude-mind"}
    override_command_options(
        command_name="create",
        command_class=_DummyCommandClass,
        params=params,
    )
    return params


# -- override_command_options hook tests --


def test_adds_all_supporting_services(mind_create_params: dict[str, Any]) -> None:
    """Verify that the plugin adds all supporting services."""
    assert len(mind_create_params["extra_window"]) == _SUPPORTING_SERVICE_COUNT


def test_adds_conv_watcher_service(mind_create_params: dict[str, Any]) -> None:
    entries = [c for c in mind_create_params["extra_window"] if CONV_WATCHER_WINDOW_NAME in c]
    assert len(entries) == 1
    assert CONV_WATCHER_COMMAND in entries[0]


def test_adds_event_watcher_service(mind_create_params: dict[str, Any]) -> None:
    prefix = f'{EVENT_WATCHER_WINDOW_NAME}="'
    entries = [c for c in mind_create_params["extra_window"] if c.startswith(prefix)]
    assert len(entries) == 1
    assert EVENT_WATCHER_COMMAND in entries[0]


def test_adds_web_server_service(mind_create_params: dict[str, Any]) -> None:
    entries = [c for c in mind_create_params["extra_window"] if WEB_SERVER_WINDOW_NAME in c]
    assert len(entries) == 1


def test_adds_observer_service(mind_create_params: dict[str, Any]) -> None:
    entries = [c for c in mind_create_params["extra_window"] if OBSERVER_WINDOW_NAME in c]
    assert len(entries) == 1
    assert OBSERVER_COMMAND in entries[0]


def test_adds_supporting_services_for_positional_agent_type() -> None:
    params: dict[str, Any] = {"extra_window": (), "positional_agent_type": "claude-mind"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["extra_window"]) == _SUPPORTING_SERVICE_COUNT


def test_does_not_modify_non_create_commands() -> None:
    params: dict[str, Any] = {"extra_window": (), "type": "claude-mind"}
    override_command_options(command_name="connect", command_class=_DummyCommandClass, params=params)
    assert params["extra_window"] == ()


def test_does_not_modify_for_other_agent_types() -> None:
    params: dict[str, Any] = {"extra_window": (), "type": "claude"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert params["extra_window"] == ()


def test_does_not_modify_when_no_agent_type() -> None:
    params: dict[str, Any] = {"extra_window": ()}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert params["extra_window"] == ()


def test_injects_supporting_services_for_registered_subclass() -> None:
    """Verify that a registered agent type that subclasses ClaudeMindAgent gets supporting services."""
    from imbue.mngr.config.agent_class_registry import register_agent_class
    from imbue.mngr.config.agent_class_registry import reset_agent_class_registry

    class _TestSubclassAgent(ClaudeMindAgent):
        """Test subclass for verifying subclass detection."""

    try:
        register_agent_class("test-subclass-82741", _TestSubclassAgent)
        params: dict[str, Any] = {"extra_window": (), "type": "test-subclass-82741"}
        override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
        assert len(params["extra_window"]) == _SUPPORTING_SERVICE_COUNT
    finally:
        reset_agent_class_registry()


def test_preserves_existing_extra_windows() -> None:
    params: dict[str, Any] = {"extra_window": ('monitor="htop"',), "type": "claude-mind"}
    override_command_options(command_name="create", command_class=_DummyCommandClass, params=params)
    assert len(params["extra_window"]) == _SUPPORTING_SERVICE_COUNT + 1
    assert params["extra_window"][0] == 'monitor="htop"'


# -- inject_supporting_services tests --


def test_inject_supporting_services_adds_all() -> None:
    """Verify that inject_supporting_services adds all expected services."""
    params: dict[str, Any] = {}
    inject_supporting_services(params)
    assert len(params["extra_window"]) == _SUPPORTING_SERVICE_COUNT


def test_inject_supporting_services_preserves_existing() -> None:
    params: dict[str, Any] = {"extra_window": ('foo="bar"',)}
    inject_supporting_services(params)
    assert len(params["extra_window"]) == _SUPPORTING_SERVICE_COUNT + 1
    assert params["extra_window"][0] == 'foo="bar"'


# -- get_agent_type_from_params tests --


def test_get_agent_type_from_params_returns_agent_type() -> None:
    assert get_agent_type_from_params({"type": "claude"}) == "claude"


def test_get_agent_type_from_params_returns_positional() -> None:
    assert get_agent_type_from_params({"positional_agent_type": "codex"}) == "codex"


def test_get_agent_type_from_params_prefers_agent_type() -> None:
    params = {"type": "claude", "positional_agent_type": "codex"}
    assert get_agent_type_from_params(params) == "claude"


def test_get_agent_type_from_params_returns_none_when_absent() -> None:
    assert get_agent_type_from_params({}) is None


# -- Web server service tests --


def test_web_server_command_is_parseable_as_named_command() -> None:
    """Verify the web server command is parseable as a NamedCommand."""
    params: dict[str, Any] = {}
    inject_supporting_services(params)
    web_entries = [c for c in params["extra_window"] if WEB_SERVER_WINDOW_NAME in c]
    assert len(web_entries) == 1
    named_cmd = NamedCommand.from_string(web_entries[0])
    assert named_cmd.window_name == WEB_SERVER_WINDOW_NAME


def test_observer_command_is_parseable_as_named_command() -> None:
    """Verify the observer command is parseable as a NamedCommand."""
    params: dict[str, Any] = {}
    inject_supporting_services(params)
    observer_entries = [c for c in params["extra_window"] if OBSERVER_WINDOW_NAME in c]
    assert len(observer_entries) == 1
    named_cmd = NamedCommand.from_string(observer_entries[0])
    assert named_cmd.window_name == OBSERVER_WINDOW_NAME


# -- modify_env_vars tests --


def _make_host_stub() -> Any:
    """Create a host stub that supports execute_idempotent_command for settings loading."""

    def _result(cmd: str, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(success=False, stdout="", stderr="")

    stub = SimpleNamespace(
        host_dir=Path("/home/user/.mngr"),
        execute_idempotent_command=_result,
        execute_stateful_command=_result,
    )
    return stub


def test_modify_env_vars_sets_uv_tool_dirs() -> None:
    """Verify that modify_env_vars sets UV_TOOL_DIR, UV_TOOL_BIN_DIR, and MNGR_LLM_MODEL."""
    host_stub = _make_host_stub()
    agent = ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
        host=host_stub,
        id="abc",
        work_dir=Path("/home/user/work"),
    )
    env_vars = {"MNGR_AGENT_STATE_DIR": "/home/user/.mngr/agents/abc"}
    agent.modify_env_vars(host_stub, env_vars)
    assert env_vars["UV_TOOL_DIR"] == "/home/user/.mngr/agents/abc/tools"
    assert env_vars["UV_TOOL_BIN_DIR"] == "/home/user/.mngr/agents/abc/bin"
    assert env_vars["MNGR_LLM_MODEL"] == "claude-haiku-4.5"


def test_modify_env_vars_noop_without_state_dir() -> None:
    """Verify that modify_env_vars does nothing for UV dirs when MNGR_AGENT_STATE_DIR is not set."""
    host_stub = _make_host_stub()
    agent = ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
        host=host_stub,
        id="abc",
        work_dir=Path("/home/user/work"),
    )
    env_vars: dict[str, str] = {}
    agent.modify_env_vars(host_stub, env_vars)
    assert "UV_TOOL_DIR" not in env_vars
    assert "UV_TOOL_BIN_DIR" not in env_vars
    # MNGR_LLM_MODEL should still be set even without state dir
    assert env_vars["MNGR_LLM_MODEL"] == "claude-haiku-4.5"


# -- assemble_command tests --


def test_assemble_command_prepends_cd_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that assemble_command prepends 'cd "$ROLE" &&' to the base command."""
    base_cmd = CommandString("claude --resume $SID || claude --session-id UUID")

    monkeypatch.setattr(ClaudeAgent, "assemble_command", lambda self, host, args, override: base_cmd)

    agent = ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
    )
    result = agent.assemble_command(cast(Any, None), (), None)

    assert str(result).startswith('cd "$ROLE" && ')
    assert str(base_cmd) in str(result)


# -- _get_role_from_env tests --


def test_get_role_from_env_returns_role() -> None:
    """Verify _get_role_from_env reads the ROLE env var from options."""
    options = CreateAgentOptions.model_construct(
        environment=AgentEnvironmentOptions(env_vars=(EnvVar(key="ROLE", value="working"),)),
    )
    assert ClaudeMindAgent._get_role_from_env(options) == "working"


def test_get_role_from_env_raises_when_missing() -> None:
    """Verify _get_role_from_env raises RuntimeError when ROLE is not set."""
    options = CreateAgentOptions.model_construct(
        environment=AgentEnvironmentOptions(env_vars=()),
    )
    with pytest.raises(RuntimeError, match="ROLE environment variable is required"):
        ClaudeMindAgent._get_role_from_env(options)


# -- _configure_role_settings tests --


def test_configure_role_settings_writes_auto_memory_directory() -> None:
    """Verify that _configure_role_settings sets autoMemoryDirectory in settings.local.json."""
    host = StubHost()
    agent = ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
        work_dir=Path("/home/user/minds/agent"),
    )

    agent._configure_role_settings(
        cast(Any, host),
        active_role="thinking",
        role_dir_abs="/home/user/minds/agent/thinking",
        settings=ProvisioningSettings(),
    )

    written = [(str(p), content) for p, content in host.written_text_files]
    settings_entries = [(p, content) for p, content in written if "settings.local.json" in p]
    assert len(settings_entries) == 1

    settings = json.loads(settings_entries[0][1])
    assert settings["autoMemoryDirectory"] == "/home/user/minds/agent/thinking/memory"


def test_configure_role_settings_includes_readiness_hooks() -> None:
    """Verify that _configure_role_settings also includes readiness hooks."""
    host = StubHost()
    agent = ClaudeMindAgent.model_construct(
        agent_config=ClaudeMindConfig(),
        work_dir=Path("/home/user/minds/agent"),
    )

    agent._configure_role_settings(
        cast(Any, host),
        active_role="thinking",
        role_dir_abs="/home/user/minds/agent/thinking",
        settings=ProvisioningSettings(),
    )

    written = [(str(p), content) for p, content in host.written_text_files]
    settings_entries = [(p, content) for p, content in written if "settings.local.json" in p]
    settings = json.loads(settings_entries[0][1])
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
