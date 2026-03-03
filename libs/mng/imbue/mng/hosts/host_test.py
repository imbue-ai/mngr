"""Unit tests for Host implementation."""

import io
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import IO
from typing import cast

import pytest
from paramiko import SSHException
from pyinfra.api.host import Host as PyinfraHost

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.errors import AgentError
from imbue.mng.errors import HostConnectionError
from imbue.mng.hosts.host import Host
from imbue.mng.hosts.host import ONBOARDING_TEXT
from imbue.mng.hosts.host import ONBOARDING_TEXT_TMUX_USER
from imbue.mng.hosts.host import _build_start_agent_shell_command
from imbue.mng.hosts.host import _is_socket_closed_os_error
from imbue.mng.hosts.host import _parse_boot_time_output
from imbue.mng.hosts.host import _parse_uptime_output
from imbue.mng.interfaces.data_types import PyinfraConnector
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng.interfaces.host import OnlineHostInterface
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


def test_get_agent_references_returns_refs_with_certified_data(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references returns refs with certified_data populated."""
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

    refs = host.get_agent_references()

    assert len(refs) == 1
    assert refs[0].agent_id == agent_id
    assert refs[0].agent_name == AgentName("test-agent")
    assert refs[0].host_id == host.id
    assert refs[0].certified_data == agent_data
    assert refs[0].agent_type == "claude"
    assert refs[0].permissions == ("read", "write")
    assert refs[0].work_dir == Path("/tmp/work")


def test_get_agent_references_returns_empty_when_no_agents_dir(
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_agent_references returns empty list when no agents directory exists."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    # Don't create agents directory
    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_missing_data_json(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips agent dirs without data.json."""
    host, agents_dir = host_with_agents_dir

    # Create agent directory without data.json
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    # Don't create data.json

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_invalid_json(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips agent dirs with invalid JSON."""
    host, agents_dir = host_with_agents_dir

    # Create agent with invalid JSON
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    (agent_dir / "data.json").write_text("not valid json {{{")

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_missing_id(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips records with missing id."""
    host, agents_dir = host_with_agents_dir

    # Create agent data without id
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"name": "test-agent"}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_missing_name(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips records with missing name."""
    host, agents_dir = host_with_agents_dir

    # Create agent data without name
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": str(agent_id)}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_invalid_id(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips records with invalid id format."""
    host, agents_dir = host_with_agents_dir

    # Create agent data with invalid id
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": "", "name": "test-agent"}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_skips_invalid_name(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips records with invalid name format."""
    host, agents_dir = host_with_agents_dir

    # Create agent data with invalid name
    agent_id = AgentId.generate()
    agent_dir = agents_dir / str(agent_id)
    agent_dir.mkdir()
    agent_data = {"id": str(agent_id), "name": ""}
    (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.get_agent_references()

    assert refs == []


def test_get_agent_references_loads_multiple_agents(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references loads all valid agents."""
    host, agents_dir = host_with_agents_dir

    # Create multiple agents
    agent_ids = [AgentId.generate() for _ in range(3)]
    for i, agent_id in enumerate(agent_ids):
        agent_dir = agents_dir / str(agent_id)
        agent_dir.mkdir()
        agent_data = {"id": str(agent_id), "name": f"agent-{i}"}
        (agent_dir / "data.json").write_text(json.dumps(agent_data))

    refs = host.get_agent_references()

    assert len(refs) == 3
    ref_ids = {ref.agent_id for ref in refs}
    assert ref_ids == set(agent_ids)


def test_get_agent_references_skips_bad_records_but_loads_good_ones(
    host_with_agents_dir: tuple[Host, Path],
) -> None:
    """Test that get_agent_references skips bad records but still loads good ones."""
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

    refs = host.get_agent_references()

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
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state stores created_branch_name in data.json."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    options = CreateAgentOptions(
        name=AgentName("test-branch-store"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options, created_branch_name="mng/my-branch")

    assert agent.get_created_branch_name() == "mng/my-branch"


def test_create_agent_state_uses_explicit_agent_id(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state uses the provided agent_id instead of generating one."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

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
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state auto-generates an agent ID when none is provided."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    options = CreateAgentOptions(
        name=AgentName("test-auto-id"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)

    assert agent.id is not None
    assert str(agent.id).startswith("agent-")


def test_create_agent_state_stores_none_created_branch_name(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that create_agent_state stores null created_branch_name when not provided."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

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


def test_is_socket_closed_os_error_matches_socket_closed_message() -> None:
    assert _is_socket_closed_os_error(OSError("Socket is closed")) is True


def test_is_socket_closed_os_error_rejects_other_os_error() -> None:
    assert _is_socket_closed_os_error(OSError("No such file or directory")) is False


def test_is_socket_closed_os_error_rejects_non_os_error() -> None:
    assert _is_socket_closed_os_error(ValueError("Socket is closed")) is False


def test_get_file_retries_on_socket_closed_and_returns_result(
    local_provider: LocalProviderInstance,
) -> None:
    """A transient socket-closed error should be transparently retried."""
    fake = _FakePyinfraHost(
        get_file_results=[
            OSError("Socket is closed"),
            True,
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert fake._get_file_call_count == 2
    assert fake.disconnect_call_count >= 1


def test_get_file_raises_file_not_found_immediately_without_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """FileNotFoundError should propagate immediately without retrying."""
    fake = _FakePyinfraHost(
        get_file_results=[
            OSError("No such file or directory: /missing.txt"),
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(FileNotFoundError, match="File not found"):
        host._get_file("/missing.txt", io.BytesIO())

    assert fake._get_file_call_count == 1


def test_get_file_disconnects_on_socket_closed_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """On socket-closed error, disconnect should be called to force a fresh reconnection."""
    fake = _FakePyinfraHost(
        get_file_results=[
            OSError("Socket is closed"),
            True,
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    host._get_file("/remote/file.txt", io.BytesIO())

    assert fake.disconnect_call_count >= 1


def test_get_file_resets_output_io_between_retry_attempts(
    local_provider: LocalProviderInstance,
) -> None:
    """Output IO should be seek(0)/truncate(0) before each retry to clear partial data."""
    io_sizes_at_call_time: list[int] = []

    class _WritingHost(_FakePyinfraHost):
        def get_file(
            self,
            remote_filename: str,
            filename_or_io: str | IO[bytes],
            remote_temp_filename: str | None = None,
        ) -> bool:
            if isinstance(filename_or_io, io.BytesIO):
                # Simulate partial write on first call (like a real SFTP transfer)
                if self._get_file_call_count == 0:
                    filename_or_io.write(b"partial data")
                io_sizes_at_call_time.append(filename_or_io.tell())
            return super().get_file(remote_filename, filename_or_io, remote_temp_filename)

    fake = _WritingHost(
        get_file_results=[
            OSError("Socket is closed"),
            True,
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    host._get_file("/remote/file.txt", io.BytesIO())

    # First call: partial write advanced position to 12
    # Second call: seek(0) + truncate(0) reset to 0
    assert io_sizes_at_call_time == [12, 0]


def test_put_file_retries_on_socket_closed_and_returns_result(
    local_provider: LocalProviderInstance,
) -> None:
    """A transient socket-closed error on put_file should be transparently retried."""
    fake = _FakePyinfraHost(
        put_file_results=[
            OSError("Socket is closed"),
            True,
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert fake._put_file_call_count == 2
    assert fake.disconnect_call_count >= 1


def test_put_file_resets_input_io_position_between_retry_attempts(
    local_provider: LocalProviderInstance,
) -> None:
    """Input IO should be seek(0) before each retry so the full content is re-read."""
    io_positions_at_call_time: list[int] = []

    class _PositionAdvancingHost(_FakePyinfraHost):
        def put_file(
            self,
            filename_or_io: str | IO[str] | IO[bytes],
            remote_filename: str,
            remote_temp_filename: str | None = None,
        ) -> bool:
            if isinstance(filename_or_io, io.BytesIO):
                # Simulate partial read advancing IO position on first call
                if self._put_file_call_count == 0:
                    filename_or_io.read(5)
                io_positions_at_call_time.append(filename_or_io.tell())
            return super().put_file(filename_or_io, remote_filename, remote_temp_filename)

    fake = _PositionAdvancingHost(
        put_file_results=[
            OSError("Socket is closed"),
            True,
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    host._put_file(io.BytesIO(b"file content here"), "/remote/file.txt")

    # First call: partial read advanced position to 5
    # Second call: seek(0) reset position to 0
    assert io_positions_at_call_time == [5, 0]


def test_put_file_propagates_non_socket_closed_os_error(
    local_provider: LocalProviderInstance,
) -> None:
    """Non-socket-closed OSErrors should propagate without retry."""
    fake = _FakePyinfraHost(
        put_file_results=[
            OSError("Permission denied"),
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(OSError, match="Permission denied"):
        host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert fake._put_file_call_count == 1


def test_get_file_wraps_ssh_exception_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """SSHException should be wrapped in HostConnectionError."""
    fake = _FakePyinfraHost(
        get_file_results=[
            SSHException("connection lost"),
        ]
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    with pytest.raises(HostConnectionError, match="Could not read file"):
        host._get_file("/remote/file.txt", io.BytesIO())
