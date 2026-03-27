"""Unit tests for Host implementation."""

import io
import json
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import IO
from typing import cast

import pytest
from paramiko import ChannelException
from paramiko import SSHException
from pyinfra.api.host import Host as PyinfraHost

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentError
from imbue.mng.errors import HostConnectionError
from imbue.mng.errors import HostDataSchemaError
from imbue.mng.errors import InvalidActivityTypeError
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.hosts.host import Host
from imbue.mng.hosts.host import ONBOARDING_TEXT
from imbue.mng.hosts.host import ONBOARDING_TEXT_TMUX_USER
from imbue.mng.hosts.host import _build_start_agent_shell_command
from imbue.mng.hosts.host import _format_env_file
from imbue.mng.hosts.host import _is_transient_ssh_error
from imbue.mng.hosts.host import _parse_boot_time_output
from imbue.mng.hosts.host import _parse_uptime_output
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentLabelOptions
from imbue.mng.interfaces.host import AgentProvisioningOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import FileModificationSpec
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.utils.testing import get_short_random_string


class _TestableAgent(BaseAgent):
    """Test agent with observable on_destroy behavior."""

    on_destroy_called: bool = False
    on_destroy_should_raise: bool = False

    def on_destroy(self, host: OnlineHostInterface) -> None:
        self.on_destroy_called = True
        if self.on_destroy_should_raise:
            raise AgentError("cleanup failed")


def _create_testable_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
    *,
    on_destroy_should_raise: bool = False,
) -> tuple[_TestableAgent, Host]:
    """Create a _TestableAgent with proper filesystem setup."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent_id = AgentId.generate()
    agent_name = AgentName(f"test-agent-{get_short_random_string()}")

    create_time = datetime.now(timezone.utc)

    # Create agent directory and data.json
    agent_dir = local_provider.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": "test",
        "command": "sleep 1000",
        "work_dir": str(temp_work_dir),
        "create_time": create_time.isoformat(),
    }
    (agent_dir / "data.json").write_text(json.dumps(data))

    agent = _TestableAgent(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("test"),
        work_dir=temp_work_dir,
        create_time=create_time,
        host_id=host.id,
        host=host,
        mng_ctx=local_provider.mng_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 1000")),
        on_destroy_should_raise=on_destroy_should_raise,
    )
    return agent, host


@pytest.fixture
def host_with_agents_dir(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
) -> tuple[Host, Path]:
    """Create a Host with an agents directory for testing."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)
    agents_dir = local_provider.host_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    return host, agents_dir


def test_discover_agents_returns_refs_with_certified_data(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents returns refs with certified_data populated."""
    host, agents_dir = host_with_agents_dir

    # Create agent data
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {
        "id": str(agent_id),
        "name": "test-agent",
        "type": "claude",
        "permissions": ["read", "write"],
        "work_dir": "/tmp/work",
    }
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert len(refs) == 1
    assert refs[0].agent_id == agent_id
    assert refs[0].agent_name == AgentName("test-agent")
    assert refs[0].host_id == host.id
    assert refs[0].certified_data == agent_data
    assert refs[0].agent_type == "claude"
    assert refs[0].permissions == ("read", "write")
    assert refs[0].work_dir == Path("/tmp/work")


def test_discover_agents_returns_empty_when_no_agents_dir(
    local_host: Host,
) -> None:
    """Test that discover_agents returns empty list when no agents directory exists."""
    host = local_host
    # Don't create agents directory
    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_missing_data_json(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips agent dirs without data.json."""
    host, agents_dir = host_with_agents_dir

    # Create agent directory without data.json
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    # Don't create data.json

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_invalid_json(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips agent dirs with invalid JSON."""
    host, agents_dir = host_with_agents_dir

    # Create agent with invalid JSON
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    (agent_dir / "data.json").write_text("not valid json {{{")

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_missing_id(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips records with missing id."""
    host, agents_dir = host_with_agents_dir

    # Create agent data without id
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"name": "test-agent"}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_missing_name(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips records with missing name."""
    host, agents_dir = host_with_agents_dir

    # Create agent data without name
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": str(agent_id)}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_invalid_id(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips records with invalid id format."""
    host, agents_dir = host_with_agents_dir

    # Create agent data with invalid id
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": "", "name": "test-agent"}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_skips_invalid_name(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips records with invalid name format."""
    host, agents_dir = host_with_agents_dir

    # Create agent data with invalid name
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": str(agent_id), "name": ""}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert refs == []


def test_discover_agents_loads_multiple_agents(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents loads all valid agents."""
    host, agents_dir = host_with_agents_dir

    # Create multiple agents
    agent_ids = [AgentId.generate() for _ in range(3)]
    for i, agent_id in enumerate(agent_ids):
        agent_dir = agents_dir / str(agent_id)
        agent_dir.mkdir()
        agent_data = {"id": str(agent_id), "name": f"agent-{i}"}
        (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.discover_agents()

    assert len(refs) == 3
    ref_ids = {ref.agent_id for ref in refs}
    assert ref_ids == set(agent_ids)


def test_discover_agents_skips_bad_records_but_loads_good_ones(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that discover_agents skips bad records but still loads good ones."""
    host, agents_dir = host_with_agents_dir

    # Create a good agent
    good_id = AgentId.generate()
    good_dir = agents_dir / str(good_id)
    good_dir.mkdir()
    (good_dir / "data.json").write_text(json.dumps({"id": str(good_id), "name": "good-agent"}))

    # Create a bad agent (missing name)
    bad_id = AgentId.generate()
    bad_dir = agents_dir / str(bad_id)
    bad_dir.mkdir()
    (bad_dir / "data.json").write_text(json.dumps({"id": str(bad_id)}))

    # Create another good agent
    good_id_2 = AgentId.generate()
    good_dir_2 = agents_dir / str(good_id_2)
    good_dir_2.mkdir()
    (good_dir_2 / "data.json").write_text(json.dumps({"id": str(good_id_2), "name": "good-agent-2"}))

    refs = host.discover_agents()

    # Should have 2 good agents, bad one skipped
    assert len(refs) == 2
    ref_ids = {ref.agent_id for ref in refs}
    assert good_id in ref_ids
    assert good_id_2 in ref_ids
    assert bad_id not in ref_ids


@pytest.mark.tmux
def test_destroy_agent_calls_on_destroy(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that destroy_agent calls agent.on_destroy() before cleanup."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir)

    agent_dir = local_provider.host_dir / "agents" / str(agent.id)
    assert agent_dir.exists()

    host.destroy_agent(agent)

    assert agent.on_destroy_called
    assert not agent_dir.exists()


@pytest.mark.tmux
def test_destroy_agent_continues_cleanup_when_on_destroy_raises(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that destroy_agent still cleans up if agent.on_destroy() raises."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir, on_destroy_should_raise=True)

    agent_dir = local_provider.host_dir / "agents" / str(agent.id)
    assert agent_dir.exists()

    # Exception propagates, but cleanup still runs
    with pytest.raises(AgentError, match="cleanup failed"):
        host.destroy_agent(agent)

    # State directory should still be cleaned up
    assert not agent_dir.exists()


# =========================================================================
# Tests for get_created_branch_name
# =========================================================================


def test_get_created_branch_name_returns_value_from_data_json(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that get_created_branch_name returns the value from data.json."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir)

    agent_dir = local_provider.host_dir / "agents" / str(agent.id)
    data = json.loads((agent_dir / "data.json").read_text())
    data["created_branch_name"] = "mng/test-branch"
    (agent_dir / "data.json").write_text(json.dumps(data))

    assert agent.get_created_branch_name() == "mng/test-branch"


def test_get_created_branch_name_returns_none_when_absent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that get_created_branch_name returns None for agents without it."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir)

    assert agent.get_created_branch_name() is None


def test_create_agent_state_stores_created_branch_name(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state stores created_branch_name in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("test-branch-store"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options, created_branch_name="mng/my-branch")

    assert agent.get_created_branch_name() == "mng/my-branch"


def test_create_agent_state_uses_explicit_agent_id(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state uses the provided agent_id instead of generating one."""
    host = local_host
    explicit_id = AgentId()
    options = CreateAgentOptions(
        agent_id=explicit_id,
        name=AgentName("test-explicit-id"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)

    assert agent.id == explicit_id


def test_create_agent_state_generates_id_when_not_provided(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state auto-generates an agent ID when none is provided."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("test-auto-id"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)

    assert agent.id is not None
    assert str(agent.id).startswith("agent-")


def test_create_agent_state_stores_none_created_branch_name(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state stores null created_branch_name when not provided."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("test-no-branch"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)

    assert agent.get_created_branch_name() is None


def test_get_created_branch_name_returns_none_when_null(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that get_created_branch_name returns None when value is null in data.json."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir)

    agent_dir = local_provider.host_dir / "agents" / str(agent.id)
    data = json.loads((agent_dir / "data.json").read_text())
    data["created_branch_name"] = None
    (agent_dir / "data.json").write_text(json.dumps(data))

    assert agent.get_created_branch_name() is None


# =========================================================================
# Tests for _build_start_agent_shell_command
# =========================================================================


def _create_test_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> BaseAgent:
    """Create a minimal test agent for command building tests."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent_id = AgentId.generate()
    agent_name = AgentName(f"test-agent-{get_short_random_string()}")

    # Create agent directory and data.json
    agent_dir = local_provider.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": "test",
        "command": "sleep 1000",
        "work_dir": str(temp_work_dir),
    }
    (agent_dir / "data.json").write_text(json.dumps(data))

    return BaseAgent(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("test"),
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        host=host,
        mng_ctx=local_provider.mng_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 1000")),
    )


def _build_command_with_defaults(
    agent: BaseAgent,
    host_dir: Path,
    additional_commands: list[NamedCommand] | None = None,
    unset_vars: list[str] | None = None,
    onboarding_text: str | None = None,
) -> str:
    """Call _build_start_agent_shell_command with standard test defaults."""
    return _build_start_agent_shell_command(
        agent=agent,
        session_name=f"mng-{agent.name}",
        command="sleep 1000",
        additional_commands=additional_commands if additional_commands is not None else [],
        env_shell_cmd="bash -c 'exec \"${MNG_SAVED_DEFAULT_TMUX_COMMAND:-bash}\"'",
        tmux_config_path=Path("/tmp/tmux.conf"),
        unset_vars=unset_vars if unset_vars is not None else [],
        host_dir=host_dir,
        onboarding_text=onboarding_text,
    )


def test_build_start_agent_shell_command_produces_single_command(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """The function should produce a single &&-chained shell command."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    assert isinstance(result, str)

    # Should contain the core tmux commands chained with &&
    assert "tmux" in result
    assert "new-session" in result
    assert "set-option" in result
    assert "default-command" in result
    assert "send-keys" in result

    # Should contain activity recording
    assert "mkdir -p" in result
    assert "activity" in result

    # Should contain the process monitor
    assert "nohup" in result
    assert "pane_pid" in result


def test_build_start_agent_shell_command_includes_unset_vars(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Unset vars should appear at the start of the command chain."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, unset_vars=["FOO_VAR", "BAR_VAR"])

    assert "unset FOO_VAR" in result
    assert "unset BAR_VAR" in result

    # Unset commands should come before tmux new-session
    unset_pos = result.index("unset")
    new_session_pos = result.index("new-session")
    assert unset_pos < new_session_pos


def test_build_start_agent_shell_command_includes_additional_windows(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Additional commands should create new tmux windows."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    additional_commands = [
        NamedCommand(command=CommandString("tail -f /var/log/syslog"), window_name="logs"),
        NamedCommand(command=CommandString("htop"), window_name=None),
    ]
    result = _build_command_with_defaults(agent, temp_host_dir, additional_commands=additional_commands)

    # Should create new windows
    assert "new-window" in result
    assert "logs" in result
    assert "cmd-2" in result

    # Should select window 0 at the end (since we have additional commands)
    assert "select-window" in result

    # Should send keys for the additional commands
    assert "tail -f /var/log/syslog" in result
    assert "htop" in result


def test_build_start_agent_shell_command_no_select_window_without_additional_commands(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """select-window should not appear when there are no additional commands."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    assert "select-window" not in result


def test_build_start_agent_shell_command_uses_and_chaining(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """All steps should be chained with && for fail-fast behavior."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    # The guard is joined with ";", the rest with "&&"
    # Split past the guard to check the && chain
    assert "; " in result
    after_guard = result.split("; ", 1)[1]
    parts = after_guard.split(" && ")
    assert len(parts) >= 7


def test_build_start_agent_shell_command_bails_if_session_exists(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """The command should start with a guard that exits early if the tmux session already exists."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)
    session_name = f"mng-{agent.name}"

    # Guard should be the first part (before the ";")
    guard, rest = result.split("; ", 1)
    assert "has-session" in guard
    assert session_name in guard
    assert "exit 0" in guard

    # The rest of the command (tmux new-session, etc.) comes after
    assert "new-session" in rest


def test_build_start_agent_shell_command_monitor_retries_pane_pid(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """The process monitor should retry getting the pane PID instead of exiting immediately."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    # The monitor script should contain retry loop elements
    assert "TRIES=0" in result
    assert "TRIES=$((TRIES + 1))" in result
    assert "sleep 1" in result


def test_build_start_agent_shell_command_default_command_uses_user_shell(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """The default-command should query the user's shell and exec into it."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    # Should query the user's original default-command via tmux show-option
    assert "show-option" in result

    # Should save the user's shell via tmux set-environment
    assert "MNG_SAVED_DEFAULT_TMUX_COMMAND" in result

    # The default-command should exec into the saved user shell, not hardcoded bash
    assert "MNG_SAVED_DEFAULT_TMUX_COMMAND:-bash" in result


def test_build_start_agent_shell_command_includes_onboarding_hook(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """When onboarding_text is provided, the output should contain set-hook with display-popup."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, onboarding_text=ONBOARDING_TEXT)

    assert "set-hook" in result
    assert "display-popup" in result
    assert "client-attached" in result


def test_build_start_agent_shell_command_no_onboarding_hook_by_default(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """When onboarding_text is None (default), no hook or popup should appear."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    assert "set-hook" not in result
    assert "display-popup" not in result
    assert "client-attached" not in result


# =========================================================================
# Tests for onboarding helpers
# =========================================================================


def test_onboarding_text_contains_keybindings() -> None:
    """The onboarding text should contain all documented keybindings."""
    assert "Ctrl-b d" in ONBOARDING_TEXT
    assert "Ctrl-b [" in ONBOARDING_TEXT
    assert "Ctrl-q" in ONBOARDING_TEXT
    assert "Ctrl-t" in ONBOARDING_TEXT
    assert "mng connect" in ONBOARDING_TEXT


def test_onboarding_text_tmux_user_contains_keybindings() -> None:
    """The tmux-user onboarding text should contain the custom keybindings and connect command."""
    assert "Ctrl-q" in ONBOARDING_TEXT_TMUX_USER
    assert "Ctrl-t" in ONBOARDING_TEXT_TMUX_USER
    assert "mng connect" in ONBOARDING_TEXT_TMUX_USER


def test_build_start_agent_shell_command_includes_onboarding_hook_tmux_user(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """When onboarding_text is ONBOARDING_TEXT_TMUX_USER, the hook should use that text."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, onboarding_text=ONBOARDING_TEXT_TMUX_USER)

    assert "set-hook" in result
    assert "display-popup" in result
    assert "client-attached" in result


# =========================================================================
# Tests for _parse_uptime_output
# =========================================================================


def test_parse_uptime_output_macos_format() -> None:
    """Test parsing macOS-style uptime output (boot timestamp + current timestamp)."""
    # macOS sysctl gives boot time, date gives current time
    stdout = "1700000000\n1700003600\n"
    result = _parse_uptime_output(stdout)
    assert result == 3600.0


def test_parse_uptime_output_linux_format() -> None:
    """Test parsing Linux-style /proc/uptime output."""
    stdout = "12345.67 98765.43\n"
    result = _parse_uptime_output(stdout)
    assert result == 12345.67


def test_parse_uptime_output_empty() -> None:
    """Test parsing empty output returns 0."""
    assert _parse_uptime_output("") == 0.0
    assert _parse_uptime_output("  \n") == 0.0


def test_parse_uptime_output_unexpected_lines() -> None:
    """Test parsing output with unexpected number of lines returns 0."""
    stdout = "line1\nline2\nline3\n"
    assert _parse_uptime_output(stdout) == 0.0


def test_parse_uptime_output_non_numeric_two_lines() -> None:
    """Test parsing non-numeric macOS-style output returns 0."""
    assert _parse_uptime_output("error\nmessage\n") == 0.0


def test_parse_uptime_output_non_numeric_single_line() -> None:
    """Test parsing non-numeric Linux-style output returns 0."""
    assert _parse_uptime_output("not_a_number\n") == 0.0


# =========================================================================
# Tests for _parse_boot_time_output
# =========================================================================


def test_parse_boot_time_output_valid_timestamp() -> None:
    """Test parsing a valid Unix timestamp returns the correct datetime."""
    # Both macOS sysctl and Linux btime produce a single Unix timestamp
    result = _parse_boot_time_output("1700000000\n")
    assert result is not None
    assert result == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_parse_boot_time_output_empty() -> None:
    """Test parsing empty output returns None."""
    assert _parse_boot_time_output("") is None
    assert _parse_boot_time_output("  \n") is None


def test_parse_boot_time_output_non_numeric() -> None:
    """Test parsing non-numeric output returns None."""
    assert _parse_boot_time_output("not_a_number\n") is None


# =========================================================================
# Tests for socket closed retry logic
# =========================================================================


class _FakePyinfraHost:
    """Test double for pyinfra Host that simulates configurable file operation behavior."""

    def __init__(
        self,
        get_file_results: list[bool | Exception] | None = None,
        put_file_results: list[bool | Exception] | None = None,
    ) -> None:
        self.connected = True
        self.name = "fake-ssh-host"
        self.connector_cls = type("SSHConnector", (), {})
        self.data: dict[str, str] = {}
        self._get_file_results: list[bool | Exception] = get_file_results or []
        self._put_file_results: list[bool | Exception] = put_file_results or []
        self._get_file_call_count = 0
        self._put_file_call_count = 0
        self.disconnect_call_count = 0

    def connect(self, raise_exceptions: bool = False) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.disconnect_call_count += 1

    def get_file(
        self,
        remote_filename: str,
        filename_or_io: str | IO[bytes],
        remote_temp_filename: str | None = None,
    ) -> bool:
        idx = self._get_file_call_count
        self._get_file_call_count += 1
        if idx < len(self._get_file_results):
            result = self._get_file_results[idx]
            if isinstance(result, Exception):
                raise result
            return result
        return True

    def put_file(
        self,
        filename_or_io: str | IO[str] | IO[bytes],
        remote_filename: str,
        remote_temp_filename: str | None = None,
    ) -> bool:
        idx = self._put_file_call_count
        self._put_file_call_count += 1
        if idx < len(self._put_file_results):
            result = self._put_file_results[idx]
            if isinstance(result, Exception):
                raise result
            return result
        return True


def _create_host_with_fake_connector(
    local_provider: LocalProviderInstance,
    fake_host: _FakePyinfraHost,
) -> Host:
    """Create a Host with a fake pyinfra connector for testing retry behavior."""
    connector = PyinfraConnector(cast(PyinfraHost, fake_host))
    return Host(
        id=HostId.generate(),
        connector=connector,
        provider_instance=local_provider,
        mng_ctx=local_provider.mng_ctx,
    )


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (OSError("Socket is closed"), True),
        (OSError("No such file or directory"), False),
        (ValueError("Socket is closed"), False),
        (SSHException("SSH session not active"), True),
        (ChannelException(2, "open failed"), True),
        (EOFError(), True),
    ],
    ids=["socket-closed", "other-os-error", "non-os-error", "ssh-exception", "channel-exception", "eof-error"],
)
def test_is_transient_ssh_error(exception: BaseException, expected: bool) -> None:
    assert _is_transient_ssh_error(exception) is expected


class _FakeTransport:
    """Fake paramiko transport for testing."""

    pass


class _BaseFakeSFTP:
    """Base class for fake SFTP clients used in tests."""

    def close(self) -> None:
        pass


class _FakeSSHClient:
    """Minimal fake paramiko SSHClient for testing the paramiko upload path."""

    def __init__(self, transport_return: object = None) -> None:
        self._transport = transport_return

    def get_transport(self) -> object:
        return self._transport


class _FakeSSHConnector:
    """Minimal fake SSH connector with a client attribute."""

    def __init__(self, client: _FakeSSHClient | None = None) -> None:
        self.client = client


class _FakeHostWithSSH(_FakePyinfraHost):
    """Fake pyinfra host that has a connector with an SSH client."""

    def __init__(
        self,
        ssh_client: _FakeSSHClient | None = None,
        get_file_results: list[bool | Exception] | None = None,
        put_file_results: list[bool | Exception] | None = None,
    ) -> None:
        super().__init__(get_file_results=get_file_results, put_file_results=put_file_results)
        self.connector = _FakeSSHConnector(client=ssh_client)


def _create_host_with_custom_sftp(
    local_provider: LocalProviderInstance,
    sftp_factory: Callable[[], object],
) -> Host:
    """Create a Host that uses a custom SFTP client factory for testing paramiko paths.

    The sftp_factory callable is invoked each time _create_sftp_client is called,
    allowing tests to inject fake SFTP behavior without monkeypatching.
    """
    host, _ = _create_host_with_custom_sftp_and_fake(local_provider, sftp_factory)
    return host


def _create_host_with_custom_sftp_and_fake(
    local_provider: LocalProviderInstance,
    sftp_factory: Callable[[], object],
) -> tuple[Host, _FakeHostWithSSH]:
    """Like _create_host_with_custom_sftp but also returns the underlying fake pyinfra host.

    This is useful for tests that need to inspect the fake host's state
    (e.g. disconnect_call_count) after exercising the Host.
    """

    class _HostWithCustomSFTP(Host):
        def _create_sftp_client(self, transport: object) -> Any:
            return sftp_factory()

    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=_FakeTransport()))
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithCustomSFTP(
        id=HostId.generate(),
        connector=connector,
        provider_instance=local_provider,
        mng_ctx=local_provider.mng_ctx,
    )
    return host, fake


@pytest.mark.parametrize(
    "exception",
    [OSError("Socket is closed"), SSHException("SSH session not active"), EOFError()],
    ids=["socket-closed", "ssh-exception", "eof-error"],
)
def test_get_file_retries_on_transient_error_and_returns_result(
    local_provider: LocalProviderInstance,
    exception: Exception,
) -> None:
    """Transient SSH errors should be transparently retried on get_file."""
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise exception

    host = _create_host_with_custom_sftp(local_provider, _FailOnceThenSucceedSFTP)
    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert call_count == 2


def test_get_file_raises_file_not_found_immediately_without_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """FileNotFoundError should propagate immediately without retrying."""

    class _NotFoundSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            raise IOError("No such file: /missing.txt")

    host = _create_host_with_custom_sftp(local_provider, _NotFoundSFTP)

    with pytest.raises(FileNotFoundError, match="File not found"):
        host._get_file("/missing.txt", io.BytesIO())


@pytest.mark.parametrize(
    "exception",
    [OSError("Socket is closed"), SSHException("SSH session not active"), EOFError()],
    ids=["socket-closed", "ssh-exception", "eof-error"],
)
def test_put_file_retries_on_transient_error_and_returns_result(
    local_provider: LocalProviderInstance,
    exception: Exception,
) -> None:
    """Transient SSH errors should be transparently retried on put_file."""
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise exception

    host = _create_host_with_custom_sftp(local_provider, _FailOnceThenSucceedSFTP)
    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert call_count == 2


def test_get_file_resets_output_io_between_retry_attempts(
    local_provider: LocalProviderInstance,
) -> None:
    """Output IO should be seek(0)/truncate(0) before each retry to clear partial data."""
    io_sizes_at_call_time: list[int] = []

    class _PartialWriteThenSucceedSFTP(_BaseFakeSFTP):
        _call_count = 0

        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            self.__class__._call_count += 1
            if self.__class__._call_count == 1:
                fl.write(b"partial data")
                io_sizes_at_call_time.append(fl.tell())
                raise OSError("Socket is closed")
            io_sizes_at_call_time.append(fl.tell())

    host = _create_host_with_custom_sftp(local_provider, _PartialWriteThenSucceedSFTP)
    host._get_file("/remote/file.txt", io.BytesIO())

    # First call: partial write advanced position to 12, then socket closed
    # Second call: seek(0) + truncate(0) reset position to 0 before creating new SFTP
    assert io_sizes_at_call_time == [12, 0]


def test_put_file_resets_input_io_position_between_retry_attempts(
    local_provider: LocalProviderInstance,
) -> None:
    """Input IO should be seek(0) before each retry so the full content is re-read."""
    io_positions_at_call_time: list[int] = []

    class _PartialReadThenSucceedSFTP(_BaseFakeSFTP):
        _call_count = 0

        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            self.__class__._call_count += 1
            if self.__class__._call_count == 1:
                fl.read(5)
                io_positions_at_call_time.append(fl.tell())
                raise OSError("Socket is closed")
            io_positions_at_call_time.append(fl.tell())

    host = _create_host_with_custom_sftp(local_provider, _PartialReadThenSucceedSFTP)
    host._put_file(io.BytesIO(b"file content here"), "/remote/file.txt")

    # First call: partial read advanced position to 5, then socket closed
    # Second call: seek(0) reset position to 0 before creating new SFTP
    assert io_positions_at_call_time == [5, 0]


def test_get_file_channel_exception_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """ChannelException should retry without calling disconnect on the connector.

    When the server refuses to open a new channel (e.g. MaxSessions limit),
    the transport is still alive.  Disconnecting would kill other threads'
    in-flight SFTP operations on the shared transport.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ChannelException(2, "open failed")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 0


def test_put_file_channel_exception_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """ChannelException should retry without calling disconnect on the connector.

    When the server refuses to open a new channel (e.g. MaxSessions limit),
    the transport is still alive.  Disconnecting would kill other threads'
    in-flight SFTP operations on the shared transport.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ChannelException(2, "open failed")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 0


def test_get_file_ssh_exception_disconnects_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """Non-ChannelException SSHException should disconnect before retrying.

    This contrasts with ChannelException which should NOT disconnect.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SSHException("SSH session not active")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 1


def test_put_file_ssh_exception_disconnects_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """Non-ChannelException SSHException should disconnect before retrying.

    This contrasts with ChannelException which should NOT disconnect.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SSHException("SSH session not active")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 1


def test_put_file_propagates_non_socket_closed_os_error(
    local_provider: LocalProviderInstance,
) -> None:
    """Non-socket-closed OSErrors should propagate without retry."""

    class _PermissionDeniedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            raise OSError("Permission denied")

    host = _create_host_with_custom_sftp(local_provider, _PermissionDeniedSFTP)

    with pytest.raises(OSError, match="Permission denied"):
        host._put_file(io.BytesIO(b"content"), "/remote/file.txt")


def test_get_paramiko_transport_raises_for_host_without_connector(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_paramiko_transport should raise when pyinfra host has no connector attribute."""
    fake = _FakePyinfraHost()
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError, match="does not support SSH"):
        host._get_paramiko_transport()


@pytest.mark.parametrize("method", ["get", "put"])
def test_file_op_raises_for_remote_host_without_ssh_client(
    local_provider: LocalProviderInstance,
    method: str,
) -> None:
    """Non-local hosts without an SSH client should fail loudly, not silently deadlock."""
    fake = _FakePyinfraHost()
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError):
        if method == "get":
            host._get_file("/remote/file.txt", io.BytesIO())
        else:
            host._put_file(io.BytesIO(b"content"), "/remote/file.txt")


@pytest.mark.parametrize("method", ["get", "put"])
def test_paramiko_raises_when_no_transport(
    local_provider: LocalProviderInstance,
    method: str,
) -> None:
    """_get/put_file_via_paramiko should raise HostConnectionError when transport is None."""
    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=None))
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError, match="No active SSH transport"):
        if method == "get":
            host._get_file_via_paramiko("/remote/file.txt", io.BytesIO())
        else:
            host._put_file_via_paramiko(io.BytesIO(b"content"), "/remote/file.txt")


def test_get_file_via_paramiko_downloads_successfully(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_file_via_paramiko should create a fresh SFTP channel and download."""

    class _FakeSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            fl.write(b"file contents")

    host = _create_host_with_custom_sftp(local_provider, _FakeSFTP)
    output = io.BytesIO()
    result = host._get_file_via_paramiko("/remote/file.txt", output)

    assert result is True
    assert output.getvalue() == b"file contents"


def test_get_file_via_paramiko_raises_file_not_found(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_file_via_paramiko should convert IOError to FileNotFoundError."""

    class _FakeSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            raise IOError("No such file")

    host = _create_host_with_custom_sftp(local_provider, _FakeSFTP)

    with pytest.raises(FileNotFoundError, match="File not found"):
        host._get_file_via_paramiko("/remote/missing.txt", io.BytesIO())


def test_get_paramiko_transport_succeeds_for_ssh_host(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_paramiko_transport should return the transport when available."""
    expected_transport = object()
    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=expected_transport))
    host = _create_host_with_fake_connector(local_provider, fake)

    assert host._get_paramiko_transport() is expected_transport


def test_get_paramiko_transport_raises_when_client_is_none(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_paramiko_transport should raise when client is None."""
    fake = _FakeHostWithSSH(ssh_client=None)
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError, match="does not support SSH"):
        host._get_paramiko_transport()


def test_get_paramiko_transport_raises_for_non_ssh_connector(
    local_provider: LocalProviderInstance,
) -> None:
    """_get_paramiko_transport should raise when connector has no client attribute."""

    class _FakeHostWithNonSSHConnector(_FakePyinfraHost):
        connector = object()

    fake = _FakeHostWithNonSSHConnector()
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError, match="does not support SSH"):
        host._get_paramiko_transport()


def test_put_file_via_paramiko_uploads_via_fresh_sftp_channel(
    local_provider: LocalProviderInstance,
) -> None:
    """_put_file_via_paramiko should create a fresh SFTP channel and upload."""
    uploaded: dict[str, bytes] = {}

    class _FakeSFTP(_BaseFakeSFTP):
        def putfo(self, fl: io.BytesIO, remote_path: str) -> None:
            uploaded[remote_path] = fl.read()

    host = _create_host_with_custom_sftp(local_provider, _FakeSFTP)
    result = host._put_file_via_paramiko(io.BytesIO(b"hello world"), "/tmp/test.txt")

    assert result is True
    assert uploaded["/tmp/test.txt"] == b"hello world"


def test_get_file_wraps_ssh_exception_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """SSHException should be wrapped in HostConnectionError.

    Overrides _get_file_with_transient_retry to raise SSHException directly
    (bypassing the retry decorator) so this test stays fast while still
    exercising _get_file's wrapping logic.
    """

    class _HostWithImmediateSSHFailure(Host):
        def _get_file_with_transient_retry(
            self,
            remote_filename: str,
            filename_or_io: str | IO[bytes],
            remote_temp_filename: str | None,
        ) -> bool:
            raise SSHException("connection lost")

    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=_FakeTransport()))
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateSSHFailure(
        id=HostId.generate(),
        connector=connector,
        provider_instance=local_provider,
        mng_ctx=local_provider.mng_ctx,
    )

    with pytest.raises(HostConnectionError, match="Could not read file"):
        host._get_file("/remote/file.txt", io.BytesIO())


# =========================================================================
# Tests for _format_env_file
# =========================================================================


def test_format_env_file_simple_values() -> None:
    """Simple values without special characters should be unquoted."""
    result = _format_env_file({"KEY": "value", "FOO": "bar"})
    assert "KEY=value" in result
    assert "FOO=bar" in result
    assert result.endswith("\n")


def test_format_env_file_quotes_values_with_spaces() -> None:
    """Values with spaces should be double-quoted."""
    result = _format_env_file({"MSG": "hello world"})
    assert 'MSG="hello world"' in result


def test_format_env_file_escapes_double_quotes() -> None:
    """Values containing double quotes should have them escaped."""
    result = _format_env_file({"MSG": 'say "hello"'})
    assert r'MSG="say \"hello\""' in result


def test_format_env_file_quotes_values_with_single_quotes() -> None:
    """Values with single quotes should be double-quoted."""
    result = _format_env_file({"MSG": "it's fine"})
    assert """MSG="it's fine\"""" in result


def test_format_env_file_quotes_values_with_newlines() -> None:
    """Values with newlines should be double-quoted."""
    result = _format_env_file({"MSG": "line1\nline2"})
    assert 'MSG="line1\nline2"' in result


def test_format_env_file_empty_dict() -> None:
    """Empty dict should produce just a newline."""
    result = _format_env_file({})
    assert result == "\n"


# =========================================================================
# Tests for Host environment methods (local host)
# =========================================================================


def test_host_get_env_vars_returns_empty_when_not_set(
    local_host: Host,
) -> None:
    """get_env_vars should return {} when no env file exists."""
    host = local_host
    assert host.get_env_vars() == {}


def test_host_set_and_get_env_vars(
    local_host: Host,
) -> None:
    """set_env_vars and get_env_vars should round-trip correctly."""
    host = local_host
    env = {"API_KEY": "secret", "DEBUG": "true"}
    host.set_env_vars(env)

    result = host.get_env_vars()
    assert result == env


def test_host_get_env_var_returns_value(
    local_host: Host,
) -> None:
    """get_env_var should return a specific env variable."""
    host = local_host
    host.set_env_vars({"FOO": "bar", "BAZ": "qux"})
    assert host.get_env_var("FOO") == "bar"
    assert host.get_env_var("NONEXISTENT") is None


def test_host_set_env_var_adds_to_existing(
    local_host: Host,
) -> None:
    """set_env_var should add a variable without clobbering existing ones."""
    host = local_host
    host.set_env_vars({"EXISTING": "value"})
    host.set_env_var("NEW_KEY", "new_value")

    assert host.get_env_var("EXISTING") == "value"
    assert host.get_env_var("NEW_KEY") == "new_value"


# =========================================================================
# Tests for Host activity methods
# =========================================================================


def test_host_record_and_get_boot_activity(
    local_host: Host,
) -> None:
    """record_activity BOOT should write a file and get_reported_activity_time should read its mtime."""
    host = local_host
    # create_host already records BOOT activity, so it should be present
    result = host.get_reported_activity_time(ActivitySource.BOOT)
    assert result is not None

    # Record again and verify the timestamp is still present
    host.record_activity(ActivitySource.BOOT)
    new_result = host.get_reported_activity_time(ActivitySource.BOOT)
    assert new_result is not None


def test_host_record_activity_rejects_non_boot(
    local_host: Host,
) -> None:
    """record_activity should reject non-BOOT activity types on a host."""
    host = local_host
    with pytest.raises(InvalidActivityTypeError, match="Only BOOT"):
        host.record_activity(ActivitySource.USER)


def test_host_get_reported_activity_content_returns_json(
    local_host: Host,
) -> None:
    """get_reported_activity_content should return JSON string with expected fields."""
    host = local_host
    host.record_activity(ActivitySource.BOOT)
    content = host.get_reported_activity_content(ActivitySource.BOOT)
    assert content is not None
    data = json.loads(content)
    assert "time" in data
    assert "host_id" in data


def test_host_get_reported_activity_content_returns_none_for_non_boot_type(
    local_host: Host,
) -> None:
    """get_reported_activity_content should return None for activity types not yet recorded."""
    host = local_host
    # SSH activity is not recorded by create_host, so it should be None
    assert host.get_reported_activity_content(ActivitySource.SSH) is None


# =========================================================================
# Tests for Host certified data methods
# =========================================================================


def test_host_get_certified_data_returns_defaults_when_no_file(
    local_host: Host,
) -> None:
    """get_certified_data should return defaults when data.json doesn't exist."""
    host = local_host
    data = host.get_certified_data()
    assert data.host_id == str(host.id)
    assert data.host_name == str(host.get_name())


def test_host_set_and_get_certified_data(
    local_host: Host,
) -> None:
    """set_certified_data and get_certified_data should round-trip correctly."""
    host = local_host
    initial_data = host.get_certified_data()
    host.set_certified_data(initial_data)

    result = host.get_certified_data()
    assert result.host_id == initial_data.host_id
    assert result.host_name == initial_data.host_name


# =========================================================================
# Tests for Host plugin data methods
# =========================================================================


def test_host_set_and_get_plugin_data(
    local_host: Host,
) -> None:
    """set_plugin_data and get_plugin_data should round-trip via certified data."""
    host = local_host
    plugin_data = {"key1": "value1", "nested": {"a": 1}}
    host.set_plugin_data("my-plugin", plugin_data)

    # Plugin data is stored in certified_data.plugin
    certified = host.get_certified_data()
    assert "my-plugin" in certified.plugin
    assert certified.plugin["my-plugin"] == plugin_data


# =========================================================================
# Tests for Host reported plugin state files
# =========================================================================


def test_host_set_and_get_reported_plugin_state_file(
    local_host: Host,
) -> None:
    """set_reported_plugin_state_file_data and get should round-trip."""
    host = local_host
    host.set_reported_plugin_state_file_data("test-plugin", "config.json", '{"hello": "world"}')
    result = host.get_reported_plugin_state_file_data("test-plugin", "config.json")
    assert result == '{"hello": "world"}'


def test_host_get_reported_plugin_state_files_returns_empty_when_no_dir(
    local_host: Host,
) -> None:
    """get_reported_plugin_state_files should return [] when no plugin dir exists."""
    host = local_host
    assert host.get_reported_plugin_state_files("nonexistent-plugin") == []


def test_host_get_reported_plugin_state_files_lists_files(
    local_host: Host,
) -> None:
    """get_reported_plugin_state_files should list all files for a plugin."""
    host = local_host
    host.set_reported_plugin_state_file_data("test-plugin", "file1.txt", "content1")
    host.set_reported_plugin_state_file_data("test-plugin", "file2.json", "content2")

    result = sorted(host.get_reported_plugin_state_files("test-plugin"))
    assert result == ["file1.txt", "file2.json"]


# =========================================================================
# Tests for Host generated work dir tracking
# =========================================================================


def test_host_add_and_check_generated_work_dir(
    local_host: Host,
) -> None:
    """_add_generated_work_dir and _is_generated_work_dir should track correctly."""
    host = local_host
    work_dir = Path("/tmp/test-workdir")
    assert host._is_generated_work_dir(work_dir) is False

    host._add_generated_work_dir(work_dir)
    assert host._is_generated_work_dir(work_dir) is True


def test_host_remove_generated_work_dir(
    local_host: Host,
) -> None:
    """_remove_generated_work_dir should remove the tracked directory."""
    host = local_host
    work_dir = Path("/tmp/test-workdir")
    host._add_generated_work_dir(work_dir)
    assert host._is_generated_work_dir(work_dir) is True

    host._remove_generated_work_dir(work_dir)
    assert host._is_generated_work_dir(work_dir) is False


# =========================================================================
# Tests for Host lock methods
# =========================================================================


def test_host_is_lock_held_returns_false_when_no_lock_file(
    local_host: Host,
) -> None:
    """is_lock_held should return False when no lock file exists."""
    host = local_host
    assert host.is_lock_held() is False


def test_host_lock_cooperatively_acquires_and_releases(
    local_host: Host,
) -> None:
    """lock_cooperatively should acquire and release the lock."""
    host = local_host
    with host.lock_cooperatively(timeout_seconds=5.0):
        assert host.is_lock_held() is True

    # After exiting the context, the lock should be released
    assert host.is_lock_held() is False


def test_host_get_reported_lock_time_returns_none_when_no_lock(
    local_host: Host,
) -> None:
    """get_reported_lock_time should return None when no lock file."""
    host = local_host
    assert host.get_reported_lock_time() is None


def test_host_get_reported_lock_time_returns_time_when_locked(
    local_host: Host,
) -> None:
    """get_reported_lock_time should return a datetime when lock file exists."""
    host = local_host
    with host.lock_cooperatively(timeout_seconds=5.0):
        result = host.get_reported_lock_time()
        assert result is not None


# =========================================================================
# Tests for Host create_agent_state with various options
# =========================================================================


def test_host_create_agent_state_with_initial_message(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """create_agent_state should store initial_message in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("msg-test-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        initial_message="Hello from test",
    )

    agent = host.create_agent_state(temp_work_dir, options)
    assert agent.get_initial_message() == "Hello from test"


def test_host_create_agent_state_with_labels(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """create_agent_state should store labels in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("label-test-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        label_options=AgentLabelOptions(labels={"env": "test", "team": "backend"}),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    labels = agent.get_labels()
    assert labels == {"env": "test", "team": "backend"}


def test_host_create_agent_state_with_resume_message(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """create_agent_state should store resume_message in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("resume-msg-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        resume_message="Resume this!",
    )

    agent = host.create_agent_state(temp_work_dir, options)
    assert agent.get_resume_message() == "Resume this!"


def test_host_create_agent_state_with_ready_timeout(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """create_agent_state should store ready_timeout_seconds in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("timeout-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        ready_timeout_seconds=30.0,
    )

    agent = host.create_agent_state(temp_work_dir, options)
    assert agent.get_ready_timeout_seconds() == 30.0


def test_host_create_agent_state_with_additional_commands(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """create_agent_state should store additional_commands in data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("extra-cmd-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        additional_commands=(NamedCommand(command=CommandString("tail -f /var/log/syslog"), window_name="logs"),),
    )

    agent = host.create_agent_state(temp_work_dir, options)

    # Verify the data.json has the additional commands
    agent_dir = temp_host_dir / "agents" / str(agent.id)
    data = json.loads((agent_dir / "data.json").read_text())
    assert len(data["additional_commands"]) == 1
    assert data["additional_commands"][0]["command"] == "tail -f /var/log/syslog"
    assert data["additional_commands"][0]["window_name"] == "logs"


# =========================================================================
# Tests for Host.get_agents
# =========================================================================


def test_host_get_agents_returns_agents(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """get_agents should return all agents on the host."""
    host = local_host
    # Create two agents
    options1 = CreateAgentOptions(
        name=AgentName("agent-one"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    options2 = CreateAgentOptions(
        name=AgentName("agent-two"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 2"),
    )
    host.create_agent_state(temp_work_dir, options1)
    host.create_agent_state(temp_work_dir, options2)

    agents = host.get_agents()
    agent_names = {str(a.name) for a in agents}
    assert "agent-one" in agent_names
    assert "agent-two" in agent_names


# =========================================================================
# Tests for Host.provision_agent
# =========================================================================


def test_host_provision_agent_basic(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """provision_agent should run through basic provisioning without errors."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("provision-test-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mng_ctx)

    # Verify env file was created with MNG-specific variables
    env_path = temp_host_dir / "agents" / str(agent.id) / "env"
    assert env_path.exists()
    env_content = env_path.read_text()
    assert "MNG_AGENT_ID" in env_content
    assert "MNG_AGENT_NAME" in env_content
    assert "MNG_AGENT_WORK_DIR" in env_content
    assert "MNG_HOST_DIR" in env_content


def test_host_provision_agent_with_env_vars(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """provision_agent should include env_vars from options."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("env-provision-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_vars=(
                EnvVar(key="CUSTOM_KEY", value="custom_value"),
                EnvVar(key="DEBUG", value="true"),
            ),
        ),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mng_ctx)

    # Verify custom env vars are in the env file
    env_path = temp_host_dir / "agents" / str(agent.id) / "env"
    env_content = env_path.read_text()
    assert "CUSTOM_KEY=custom_value" in env_content
    assert "DEBUG=true" in env_content


def test_host_provision_agent_with_extra_provision_commands(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """provision_agent should run extra provision commands."""
    host = local_host
    # Create a marker file via extra provision command to verify execution
    marker_file = temp_work_dir / "provision_marker.txt"

    options = CreateAgentOptions(
        name=AgentName("cmd-provision-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            extra_provision_commands=(f"echo 'provisioned' > {marker_file}",),
        ),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mng_ctx)

    assert marker_file.exists()
    assert "provisioned" in marker_file.read_text()


def test_host_provision_agent_with_append_to_file(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """provision_agent should append text to files."""
    host = local_host
    target_file = temp_work_dir / "bashrc"
    target_file.write_text("existing content\n")

    options = CreateAgentOptions(
        name=AgentName("append-provision-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            append_to_files=(FileModificationSpec(remote_path=target_file, text="appended line\n"),),
        ),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mng_ctx)

    assert target_file.read_text() == "existing content\nappended line\n"


def test_host_provision_agent_with_create_directories(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """provision_agent should create directories."""
    host = local_host
    new_dir = temp_work_dir / "created_dir"

    options = CreateAgentOptions(
        name=AgentName("dir-provision-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(new_dir,),
        ),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mng_ctx)

    assert new_dir.is_dir()


# =========================================================================
# Tests for Host._get_agent_command
# =========================================================================


def test_host_get_agent_command_returns_command(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_command should return the command from data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("cmd-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 42"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    result = host._get_agent_command(agent)
    assert result == "sleep 42"


def test_host_get_agent_command_raises_when_no_data_file(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_command should raise when data.json is missing."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("no-data-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    # Remove the data.json file
    data_path = temp_host_dir / "agents" / str(agent.id) / "data.json"
    data_path.unlink()

    with pytest.raises(NoCommandDefinedError):
        host._get_agent_command(agent)


# =========================================================================
# Tests for Host._get_agent_additional_commands
# =========================================================================


def test_host_get_agent_additional_commands_returns_commands(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_additional_commands should parse commands from data.json."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("addl-cmd-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        additional_commands=(
            NamedCommand(command=CommandString("tail -f /var/log/syslog"), window_name="logs"),
            NamedCommand(command=CommandString("htop"), window_name=None),
        ),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    result = host._get_agent_additional_commands(agent)
    assert len(result) == 2
    assert result[0].command == "tail -f /var/log/syslog"
    assert result[0].window_name == "logs"
    assert result[1].command == "htop"
    assert result[1].window_name is None


def test_host_get_agent_additional_commands_returns_empty_when_none(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_additional_commands should return empty list when no additional commands."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("no-addl-cmd-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    result = host._get_agent_additional_commands(agent)
    assert result == []


def test_host_get_agent_additional_commands_handles_old_format(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_additional_commands should handle the old string format."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("old-format-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    # Manually write old-format additional_commands (list of strings)
    data_path = temp_host_dir / "agents" / str(agent.id) / "data.json"
    data = json.loads(data_path.read_text())
    data["additional_commands"] = ["tail -f /var/log/syslog", "htop"]
    data_path.write_text(json.dumps(data, indent=2))

    result = host._get_agent_additional_commands(agent)
    assert len(result) == 2
    assert result[0].command == "tail -f /var/log/syslog"
    assert result[0].window_name is None


def test_host_get_agent_additional_commands_returns_empty_when_no_file(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_additional_commands should return empty when data.json is missing."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("missing-file-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    # Remove the data.json file
    data_path = temp_host_dir / "agents" / str(agent.id) / "data.json"
    data_path.unlink()

    result = host._get_agent_additional_commands(agent)
    assert result == []


# =========================================================================
# Tests for Host._get_agent_by_id
# =========================================================================


def test_host_get_agent_by_id_returns_agent(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_get_agent_by_id should return the agent when it exists."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("id-lookup-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    result = host._get_agent_by_id(agent.id)
    assert result is not None
    assert result.id == agent.id


def test_host_get_agent_by_id_returns_none_when_not_found(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """_get_agent_by_id should return None when agent doesn't exist."""
    host = local_host
    result = host._get_agent_by_id(AgentId.generate())
    assert result is None


# =========================================================================
# Tests for Host._create_host_tmux_config
# =========================================================================


def test_host_create_host_tmux_config_creates_file(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """_create_host_tmux_config should create a tmux config file with keybindings."""
    host = local_host
    config_path = host._create_host_tmux_config()
    assert config_path.exists()

    content = config_path.read_text()
    assert "source-file" in content
    assert "C-q" in content
    assert "C-t" in content


# =========================================================================
# Tests for Host._build_env_shell_command
# =========================================================================


def test_host_build_env_shell_command_returns_bash_command(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_build_env_shell_command should return a bash -c command that sources env files."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("env-cmd-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    result = host._build_env_shell_command(agent)
    assert result.startswith("bash -c ")
    assert "MNG_SAVED_DEFAULT_TMUX_COMMAND" in result


# =========================================================================
# Tests for Host._collect_agent_env_vars
# =========================================================================


def test_host_collect_agent_env_vars_includes_mng_variables(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_collect_agent_env_vars should include MNG-specific variables."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("env-collect-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    env = host._collect_agent_env_vars(agent, options)
    assert env["MNG_HOST_DIR"] == str(temp_host_dir)
    assert env["MNG_AGENT_ID"] == str(agent.id)
    assert env["MNG_AGENT_NAME"] == str(agent.name)
    assert env["MNG_AGENT_WORK_DIR"] == str(temp_work_dir)
    assert "MNG_AGENT_STATE_DIR" in env
    assert "LLM_USER_PATH" in env
    assert "GIT_BASE_BRANCH" in env


def test_host_collect_agent_env_vars_with_env_file(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    tmp_path: Path,
) -> None:
    """_collect_agent_env_vars should load env vars from env_files."""
    host = local_host
    # Create an env file
    env_file = tmp_path / "test.env"
    env_file.write_text("FROM_FILE=file_value\n")

    options = CreateAgentOptions(
        name=AgentName("env-file-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_files=(env_file,),
        ),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    env = host._collect_agent_env_vars(agent, options)
    assert env["FROM_FILE"] == "file_value"


# =========================================================================
# Tests for Host._write_agent_env_file
# =========================================================================


def test_host_write_agent_env_file_creates_env_file(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_write_agent_env_file should create the env file."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("write-env-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    env_vars = {"KEY1": "value1", "KEY2": "value2"}
    host._write_agent_env_file(agent, env_vars)

    env_path = host.get_agent_env_path(agent)
    assert env_path.exists()
    content = env_path.read_text()
    assert "KEY1=value1" in content
    assert "KEY2=value2" in content


def test_host_write_agent_env_file_skips_when_empty(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_write_agent_env_file should not create a file for empty env vars."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("empty-env-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    host._write_agent_env_file(agent, {})

    env_path = host.get_agent_env_path(agent)
    assert not env_path.exists()


# =========================================================================
# Tests for Host.get_certified_data schema error
# =========================================================================


def test_host_get_certified_data_raises_on_invalid_json(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """get_certified_data should raise HostDataSchemaError for invalid data.json."""
    host = local_host
    # Write invalid data.json (missing required fields)
    data_path = temp_host_dir / "data.json"
    data_path.write_text('{"invalid_field": "oops", "created_at": "not-a-datetime"}')

    with pytest.raises(HostDataSchemaError):
        host.get_certified_data()
