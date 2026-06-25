"""Unit tests for Host implementation."""

import io
import json
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import IO
from typing import cast

import pluggy
import pytest
from paramiko import ChannelException
from paramiko import SSHException
from pyinfra.api.command import StringCommand
from pyinfra.api.host import Host as PyinfraHost
from pyinfra.connectors.util import CommandOutput

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import TmuxConfig
from imbue.mngr.config.data_types import WorkDirExtraPathMode
from imbue.mngr.errors import AgentError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import CommandTimeoutError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostDataSchemaError
from imbue.mngr.errors import InvalidActivityTypeError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import ONBOARDING_TEXT
from imbue.mngr.hosts.host import ONBOARDING_TEXT_TMUX_USER
from imbue.mngr.hosts.host import _LOCK_ACQUIRED_MARKER
from imbue.mngr.hosts.host import _LOCK_TIMED_OUT_MARKER
from imbue.mngr.hosts.host import _TMUX_SET_TITLES_STRING
from imbue.mngr.hosts.host import _TMUX_STATUS_LEFT_LENGTH
from imbue.mngr.hosts.host import _build_remote_lock_command
from imbue.mngr.hosts.host import _build_start_agent_shell_command
from imbue.mngr.hosts.host import _format_env_file
from imbue.mngr.hosts.host import _is_transient_ssh_error
from imbue.mngr.hosts.host import _merge_agent_type_provisioning
from imbue.mngr.hosts.host import _parse_boot_time_output
from imbue.mngr.hosts.host import _parse_uptime_output
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import AgentProvisioningOptions
from imbue.mngr.interfaces.host import AgentTmuxOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import NamedCommand
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.host import UploadFileSpec
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import TmuxHeight
from imbue.mngr.primitives import TmuxWidth
from imbue.mngr.primitives import TmuxWindowSize
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import get_cleanup_failures
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import make_test_agent_details


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
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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
        "type": "generic",
        "command": "sleep 1000",
        "work_dir": str(temp_work_dir),
        "create_time": create_time.isoformat(),
    }
    (agent_dir / "data.json").write_text(json.dumps(data))

    agent = _TestableAgent(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("generic"),
        work_dir=temp_work_dir,
        create_time=create_time,
        host_id=host.id,
        host=host,
        mngr_ctx=local_provider.mngr_ctx,
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
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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


@pytest.mark.allow_warnings(match=r"^Could not load agent reference from")
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


@pytest.mark.allow_warnings(match=r"^Skipping malformed agent record for host")
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


@pytest.mark.allow_warnings(match=r"^Skipping malformed agent record for host")
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


@pytest.mark.allow_warnings(match=r"^Skipping malformed agent record for host")
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


@pytest.mark.allow_warnings(match=r"^Skipping malformed agent record for host")
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


@pytest.mark.allow_warnings(match=r"^Skipping malformed agent record for host")
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
    """destroy_agent records an on_destroy failure but still completes the teardown.

    A MngrError raised by the on_destroy hook is captured as an OTHER cleanup failure
    (aggregated into the CleanupFailedGroup, not propagated immediately) so the remaining
    teardown steps still run.
    """
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir, on_destroy_should_raise=True)

    agent_dir = local_provider.host_dir / "agents" / str(agent.id)
    assert agent_dir.exists()

    # The hook failure is recorded and surfaced via CleanupFailedGroup rather than aborting.
    failures = get_cleanup_failures(lambda: host.destroy_agent(agent))

    assert agent.on_destroy_called
    # Exactly one failure: the on_destroy hook error, classified as OTHER and tagged
    # with the agent. The stop of a freshly-created agent with no live session is benign
    # and contributes no failures.
    assert len(failures) == 1
    on_destroy_failure = failures[0]
    assert on_destroy_failure.category == CleanupFailureCategory.OTHER
    assert on_destroy_failure.agent_name == agent.name
    assert "cleanup failed" in on_destroy_failure.message

    # State directory should still be cleaned up despite the hook failure.
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
    data["created_branch_name"] = "mngr/test-branch"
    (agent_dir / "data.json").write_text(json.dumps(data))

    assert agent.get_created_branch_name() == "mngr/test-branch"


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

    agent = host.create_agent_state(temp_work_dir, options, created_branch_name="mngr/my-branch")

    assert agent.get_created_branch_name() == "mngr/my-branch"


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


def test_create_agent_state_update_preserves_create_time(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """In update mode, create_agent_state preserves the original create_time."""
    host = local_host

    # First, create the agent normally
    original_options = CreateAgentOptions(
        name=AgentName("test-update-time"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    original_agent = host.create_agent_state(temp_work_dir, original_options)
    original_create_time = original_agent.create_time

    # Now update the agent with is_update=True
    update_options = CreateAgentOptions(
        agent_id=original_agent.id,
        name=AgentName("test-update-time"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 2"),
        is_update=True,
    )
    updated_agent = host.create_agent_state(temp_work_dir, update_options)

    assert updated_agent.id == original_agent.id
    assert updated_agent.create_time == original_create_time
    assert str(updated_agent.get_command()) == "sleep 2"


def test_create_agent_state_update_overwrites_data(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """In update mode, create_agent_state overwrites data.json with new values."""
    host = local_host

    # First, create the agent normally
    original_options = CreateAgentOptions(
        name=AgentName("test-update-data"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        label_options=AgentLabelOptions(labels={"project": "old-project"}),
    )
    original_agent = host.create_agent_state(temp_work_dir, original_options)

    # Now update with different labels
    update_options = CreateAgentOptions(
        agent_id=original_agent.id,
        name=AgentName("test-update-data"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        label_options=AgentLabelOptions(labels={"project": "new-project"}),
        is_update=True,
    )
    updated_agent = host.create_agent_state(temp_work_dir, update_options)

    assert updated_agent.get_labels() == {"project": "new-project"}


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
# Tests for _ensure_work_dir_exists
# =========================================================================


def test_ensure_work_dir_exists_succeeds_when_dir_exists(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_ensure_work_dir_exists should be a no-op when the directory exists."""
    agent, host = _create_testable_agent(local_provider, temp_host_dir, temp_work_dir)
    host._ensure_work_dir_exists(agent)


def test_ensure_work_dir_exists_raises_when_no_branch(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """_ensure_work_dir_exists should raise with plain message when no branch is recorded."""
    missing_dir = tmp_path / "nonexistent"
    agent, host = _create_testable_agent(local_provider, temp_host_dir, missing_dir)

    with pytest.raises(AgentStartError, match="does not exist"):
        host._ensure_work_dir_exists(agent)


def test_ensure_work_dir_exists_raises_with_recovery_command(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    tmp_path: Path,
) -> None:
    """_ensure_work_dir_exists should include a git worktree add command when branch is known."""
    missing_dir = tmp_path / "worktrees" / "gone"
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    options = CreateAgentOptions(
        name=AgentName("test-recovery-cmd"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(missing_dir, options, created_branch_name="mngr/my-branch")

    with pytest.raises(AgentStartError, match="git worktree add.*mngr/my-branch"):
        host._ensure_work_dir_exists(agent)


# =========================================================================
# Tests for _build_start_agent_shell_command
# =========================================================================


def _create_test_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> BaseAgent:
    """Create a minimal test agent for command building tests."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    agent_id = AgentId.generate()
    agent_name = AgentName(f"test-agent-{get_short_random_string()}")

    # Create agent directory and data.json
    agent_dir = local_provider.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": "generic",
        "command": "sleep 1000",
        "work_dir": str(temp_work_dir),
    }
    (agent_dir / "data.json").write_text(json.dumps(data))

    return BaseAgent(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("generic"),
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        host=host,
        mngr_ctx=local_provider.mngr_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 1000")),
    )


def _build_command_with_defaults(
    agent: BaseAgent,
    host_dir: Path,
    additional_commands: list[NamedCommand] | None = None,
    unset_vars: list[str] | None = None,
    onboarding_text: str | None = None,
    primary_window_name: str = "agent",
    tmux_options: AgentTmuxOptions | None = None,
) -> str:
    """Call _build_start_agent_shell_command with standard test defaults."""
    return _build_start_agent_shell_command(
        agent=agent,
        session_name=f"mngr-{agent.name}",
        command="sleep 1000",
        additional_commands=additional_commands if additional_commands is not None else [],
        env_shell_cmd="bash -c 'exec \"${MNGR_SAVED_DEFAULT_TMUX_COMMAND:-${SHELL:-bash}}\"'",
        tmux_config_path=Path("/tmp/tmux.conf"),
        unset_vars=unset_vars if unset_vars is not None else [],
        host_dir=host_dir,
        primary_window_name=primary_window_name,
        tmux_options=tmux_options if tmux_options is not None else AgentTmuxOptions(),
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


def test_build_start_agent_shell_command_names_primary_window_and_targets_it_by_name(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """new-session must name the primary window (-n) and all targets must use that name,
    not the literal :0, so mngr works regardless of the user's tmux base-index.
    """
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, primary_window_name="agent")

    session = f"mngr-{agent.name}"
    assert "new-session -d" in result
    assert " -n agent" in result
    # Targets address the window by name, never the literal :0 index.
    assert f"={session}:agent" in result
    assert f"={session}:0" not in result


def test_build_start_agent_shell_command_uses_custom_primary_window_name(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """A custom tmux.primary_window_name flows into both -n and the window targets."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, primary_window_name="primary")

    session = f"mngr-{agent.name}"
    assert " -n primary" in result
    assert f"={session}:primary" in result
    assert f"={session}:agent" not in result


def test_build_start_agent_shell_command_uses_default_dimensions_when_unset(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """With default (all-None) tmux options, the historical 200x50 size is used and no resize policy is set."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir, tmux_options=AgentTmuxOptions())

    assert "-x 200 -y 50" in result
    assert "window-size" not in result


def test_build_start_agent_shell_command_uses_custom_dimensions(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Custom width/height are passed to new-session's -x/-y."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    options = AgentTmuxOptions(width=TmuxWidth(2048), height=TmuxHeight(256))
    result = _build_command_with_defaults(agent, temp_host_dir, tmux_options=options)

    assert "-x 2048 -y 256" in result


def test_build_start_agent_shell_command_sets_manual_window_size_on_agent_window(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """A manual window-size emits a set-option targeting the agent's named primary window."""
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    options = AgentTmuxOptions(window_size=TmuxWindowSize.MANUAL)
    result = _build_command_with_defaults(agent, temp_host_dir, tmux_options=options)

    assert f"set-option -t =mngr-{agent.name}:agent window-size manual" in result


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


def test_build_start_agent_shell_command_sources_config_after_new_session(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """mngr's config is sourced at agent creation, after new-session.

    The user's own config is pulled in at tmux server start; mngr's is not, so it
    is sourced explicitly. The step is non-fatal: it is wrapped in a subshell that
    scopes '|| true' to this step alone, so a cosmetic-config error does not abort
    the agent-start chain (and a failure of an earlier step is not masked).
    """
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    result = _build_command_with_defaults(agent, temp_host_dir)

    assert result.index("new-session") < result.index("source-file")
    # Wrapped in a subshell so '|| true' scopes to this step alone.
    assert "(tmux source-file" in result
    assert "|| true)" in result


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


def test_build_start_agent_shell_command_send_keys_uses_end_of_options_separator(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Every `tmux send-keys -l` invocation must include `--` before the literal payload.

    Without the `--` end-of-options separator, tmux's argv parser treats a leading
    dash in the payload as a flag and errors with `invalid flag --`. The `send-keys
    ... Enter` calls use a key name (not -l) and are unaffected.
    """
    agent = _create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    additional_commands = [
        NamedCommand(command=CommandString("--model gemma"), window_name="dash-cmd"),
    ]
    result = _build_start_agent_shell_command(
        agent=agent,
        session_name=f"mngr-{agent.name}",
        command="--flag-leading-command",
        additional_commands=additional_commands,
        env_shell_cmd="bash -c 'true'",
        tmux_config_path=Path("/tmp/tmux.conf"),
        unset_vars=[],
        host_dir=temp_host_dir,
        primary_window_name="agent",
        tmux_options=AgentTmuxOptions(),
    )

    send_keys_l_lines = [line for line in result.split(" && ") if "send-keys" in line and " -l " in line]
    assert send_keys_l_lines, "expected at least one `tmux send-keys -l` invocation"
    for line in send_keys_l_lines:
        assert " -l -- " in line, f"missing `--` end-of-options separator in: {line}"


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
    session_name = f"mngr-{agent.name}"

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
    assert "MNGR_SAVED_DEFAULT_TMUX_COMMAND" in result

    # The default-command should exec into the saved user shell, falling back to $SHELL
    assert "MNGR_SAVED_DEFAULT_TMUX_COMMAND:-${SHELL:-bash}" in result


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
    assert "mngr connect" in ONBOARDING_TEXT


def test_onboarding_text_tmux_user_contains_keybindings() -> None:
    """The tmux-user onboarding text should contain the custom keybindings and connect command."""
    assert "Ctrl-q" in ONBOARDING_TEXT_TMUX_USER
    assert "Ctrl-t" in ONBOARDING_TEXT_TMUX_USER
    assert "mngr connect" in ONBOARDING_TEXT_TMUX_USER


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
        run_shell_command_results: list[tuple[bool, CommandOutput] | Exception] | None = None,
    ) -> None:
        self.connected = True
        self.name = "fake-ssh-host"
        self.connector_cls = type("SSHConnector", (), {})
        self.data: dict[str, str] = {}
        self._get_file_results: list[bool | Exception] = get_file_results or []
        self._put_file_results: list[bool | Exception] = put_file_results or []
        self._run_shell_command_results: list[tuple[bool, CommandOutput] | Exception] = run_shell_command_results or []
        self._get_file_call_count = 0
        self._put_file_call_count = 0
        self._run_shell_command_call_count = 0
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

    def run_shell_command(
        self,
        command: StringCommand,
        **kwargs: Any,
    ) -> tuple[bool, CommandOutput]:
        idx = self._run_shell_command_call_count
        self._run_shell_command_call_count += 1
        if idx < len(self._run_shell_command_results):
            result = self._run_shell_command_results[idx]
            if isinstance(result, Exception):
                raise result
            return result
        return True, CommandOutput([])


def _create_host_with_fake_connector(
    local_provider: LocalProviderInstance,
    fake_host: _FakePyinfraHost,
) -> Host:
    """Create a Host with a fake pyinfra connector for testing retry behavior."""
    connector = PyinfraConnector(cast(PyinfraHost, fake_host))
    return Host(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )


def _make_stop_agents_test_host(
    local_provider: LocalProviderInstance,
    agent: AgentInterface,
    command_handler: Callable[[str], CommandResult],
) -> tuple[Host, list[tuple[str, float | None]]]:
    """Build a Host whose stop-path shell commands are served by ``command_handler``.

    The returned Host's ``execute_idempotent_command`` records every
    ``(command, timeout_seconds)`` pair into the returned list (so callers can
    assert on the bounds passed) and delegates the actual result to
    ``command_handler``, which is free to return canned output or raise to
    simulate a wedged command. ``_get_agent_by_id`` is stubbed to return
    ``agent`` so ``stop_agents`` walks its full collect-then-kill sequence
    without touching real tmux or processes.
    """
    recorded_timeouts: list[tuple[str, float | None]] = []

    class _StopAgentsTestHost(Host):
        def execute_idempotent_command(
            self,
            command: str,
            user: str | None = None,
            cwd: Path | None = None,
            env: Any = None,
            timeout_seconds: float | None = None,
            raise_on_timeout: bool = False,
        ) -> CommandResult:
            recorded_timeouts.append((command, timeout_seconds))
            return command_handler(command)

        def _get_agent_by_id(self, agent_id: AgentId) -> AgentInterface | None:
            return agent

    host = _StopAgentsTestHost(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=PyinfraConnector(cast(PyinfraHost, _FakePyinfraHost())),
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )
    return host, recorded_timeouts


def test_stop_agents_bounds_every_command_with_a_timeout(
    local_provider: LocalProviderInstance,
) -> None:
    """stop_agents must pass a timeout to every shell command it runs.

    Regression for an offload hang: a wedged ``tmux list-panes`` client (tmux
    occasionally fails to return under CI load) blocked the unbounded command in
    the stop path forever, stalling the whole test batch. Every step in the
    stop/cleanup path must carry a timeout so a wedged command can't hang
    cleanup. This records the timeout passed to each command and asserts none is
    unbounded.
    """
    agent = make_test_agent_details("cleanup-timeout-agent")
    fake_pid = "12345"

    def handle(command: str) -> CommandResult:
        # Canned output (a window index for list-windows and a pane PID for
        # list-panes) so stop_agents walks its full collect-then-kill sequence.
        if "list-windows" in command:
            stdout = "0"
        elif "list-panes" in command:
            stdout = fake_pid
        else:
            stdout = ""
        return CommandResult(stdout=stdout, stderr="", success=True)

    host, recorded = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle)

    host.stop_agents([agent.id])

    # Sanity: we actually exercised the tmux pid-collection path and the kill step.
    assert any("list-panes" in command for command, _ in recorded)
    assert any("kill-session" in command for command, _ in recorded)
    # The regression guard: no command in the stop path may be unbounded.
    unbounded = [command for command, timeout in recorded if timeout is None]
    assert unbounded == [], f"stop_agents ran command(s) without a timeout: {unbounded}"


@pytest.mark.allow_warnings(match=r"^Cleanup step timed out on host")
def test_stop_agents_records_timeout_failure_and_continues(
    local_provider: LocalProviderInstance,
) -> None:
    """A timed-out stop-path command is recorded as a TIMEOUT failure and returned,
    not silently swallowed -- and cleanup continues (aggregate, not fail-fast).

    Simulates execute_idempotent_command raising CommandTimeoutError (which
    _run_classified_cleanup_command catches and converts to a TIMEOUT failure).
    The wedged list-panes does not abort: the session teardown is still attempted.
    """
    agent = make_test_agent_details("cleanup-timeout-agent")

    def handle(command: str) -> CommandResult:
        if "list-panes" in command:
            raise CommandTimeoutError(f"Command timed out after 10.0s: {command}")
        stdout = "0" if "list-windows" in command else ""
        return CommandResult(stdout=stdout, stderr="", success=True)

    host, recorded = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle)

    failures = get_cleanup_failures(lambda: host.stop_agents([agent.id]))

    assert [f.category for f in failures] == [CleanupFailureCategory.TIMEOUT]
    commands = [command for command, _ in recorded]
    assert any("list-panes" in command for command in commands)
    # Aggregate-and-continue: the timeout does not abort; kill-session is still attempted.
    assert any("kill-session" in command for command in commands)


@pytest.mark.allow_warnings(match=r"^Cleanup step left a resource behind on host")
def test_stop_agents_classifies_real_vs_benign_stderr(
    local_provider: LocalProviderInstance,
) -> None:
    """Stderr signalling 'already gone' (tmux can't-find-session, kill ESRCH) is benign
    (no failure); any other stderr line is a real failure.
    """
    agent = make_test_agent_details("cleanup-classify-agent")

    def handle_benign(command: str) -> CommandResult:
        # Session already gone (list-windows) and the server going away during teardown
        # (kill-session racing with the local tmux server exiting) are both benign.
        if "list-windows" in command:
            return CommandResult(stdout="", stderr="can't find session: mngr_x", success=False)
        if "kill-session" in command:
            return CommandResult(stdout="", stderr="lost server", success=False)
        return CommandResult(stdout="", stderr="", success=True)

    benign_host, _ = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle_benign)
    assert get_cleanup_failures(lambda: benign_host.stop_agents([agent.id])) == []

    def handle_real(command: str) -> CommandResult:
        if "list-windows" in command:
            return CommandResult(stdout="0", stderr="", success=True)
        if "list-panes" in command:
            return CommandResult(stdout="999", stderr="", success=True)
        if "kill -TERM" in command or "kill -KILL" in command:
            # A process that cannot be killed -> a real PROCESSES_REMAIN failure.
            return CommandResult(stdout="", stderr="kill: (999): Operation not permitted", success=False)
        return CommandResult(stdout="", stderr="", success=True)

    real_host, _ = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle_real)
    failures = get_cleanup_failures(lambda: real_host.stop_agents([agent.id]))
    assert [f.category for f in failures] == [CleanupFailureCategory.PROCESSES_REMAIN]


def test_stop_agents_treats_tmux_no_current_target_as_benign(
    local_provider: LocalProviderInstance,
) -> None:
    """tmux 'no current target' on kill-session is benign, not a LOCAL_STATE_REMAINS failure.

    When several agents are destroyed at once, killing the last session makes the tmux server
    exit; a concurrent kill-session against the already-gone session then reports "no current
    target". The session is gone either way, so this must not surface as a spurious cleanup
    failure (it did, flakily, before "no current target" was whitelisted).
    """
    agent = make_test_agent_details("cleanup-no-current-target-agent")

    def handle(command: str) -> CommandResult:
        if "kill-session" in command:
            return CommandResult(stdout="", stderr="no current target", success=False)
        return CommandResult(stdout="", stderr="", success=True)

    host, _ = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle)
    assert get_cleanup_failures(lambda: host.stop_agents([agent.id])) == []


def test_reap_agent_process_tree_kills_pane_and_env_marked_orphans_but_not_the_session(
    local_provider: LocalProviderInstance,
) -> None:
    """reap_agent_process_tree kills the pane descendants AND the MNGR_AGENT_ID-tagged
    orphans (reparented to PID 1), with SIGTERM then SIGKILL, but does NOT kill the
    tmux session itself.

    Regression: a long-lived daemon launched under an agent (the FCT bootstrap's
    supervisord and its ttyd) could be orphaned to PID 1 on an abrupt teardown and
    outlive the agent, holding a fixed port (EADDRINUSE on the next relaunch). The
    shared reap must catch such orphans via the env marker, which a pane/tree walk
    misses. It must NOT kill the session, so callers can reap-then-relaunch in place.
    """
    agent = make_test_agent_details("reap-tree-agent")
    pane_pid = "55502"
    orphan_pid = "55501"

    def handle(command: str) -> CommandResult:
        if "list-windows" in command:
            return CommandResult(stdout="0", stderr="", success=True)
        if "list-panes" in command:
            return CommandResult(stdout=pane_pid, stderr="", success=True)
        # The env-marker /proc scan that finds orphans reparented to PID 1.
        if "MNGR_AGENT_ID" in command:
            return CommandResult(stdout=orphan_pid, stderr="", success=True)
        return CommandResult(stdout="", stderr="", success=True)

    host, recorded = _make_stop_agents_test_host(local_provider, cast(AgentInterface, agent), handle)

    failures = host.reap_agent_process_tree(cast(AgentInterface, agent))

    assert failures == []
    commands = [command for command, _ in recorded]
    # It ran the env-marker orphan scan.
    assert any("MNGR_AGENT_ID" in command for command in commands)
    # It killed BOTH the pane pid and the env-marked orphan, via SIGTERM and SIGKILL.
    kill_commands = " ".join(c for c in commands if "kill -TERM" in c or "kill -KILL" in c)
    assert "kill -TERM" in kill_commands and "kill -KILL" in kill_commands
    assert pane_pid in kill_commands and orphan_pid in kill_commands
    # It must NOT kill the tmux session -- that's stop_agents' job, not the reap's.
    assert not any("kill-session" in command for command in commands)


def test_execute_idempotent_command_raises_command_timeout_error_on_local_timeout(
    local_host: Host,
) -> None:
    """raise_on_timeout normalizes a local timeout into a loud CommandTimeoutError.

    Exercises the real local backend: a command that outlives its timeout is
    killed and, with raise_on_timeout=True, surfaces as CommandTimeoutError
    (a MngrError) rather than the default failed CommandResult. The default
    (raise_on_timeout=False) path is asserted too, to confirm existing callers
    still see success=False on a timeout.
    """
    # Default: a local timeout is reported as a failed result, not raised.
    result = local_host.execute_idempotent_command("sleep 10", timeout_seconds=1)
    assert result.success is False

    # Opt-in: the same timeout is raised loudly as CommandTimeoutError.
    with pytest.raises(CommandTimeoutError):
        local_host.execute_idempotent_command("sleep 10", timeout_seconds=1, raise_on_timeout=True)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (OSError("Socket is closed"), True),
        (OSError("No such file or directory"), False),
        (ValueError("Socket is closed"), False),
        (SSHException("SSH session not active"), True),
        (ChannelException(2, "open failed"), True),
        (EOFError(), True),
        (TimeoutError("Timed out reading output"), True),
    ],
    ids=[
        "socket-closed",
        "other-os-error",
        "non-os-error",
        "ssh-exception",
        "channel-exception",
        "eof-error",
        "timeout-error",
    ],
)
def test_is_transient_ssh_error(exception: BaseException, expected: bool) -> None:
    assert _is_transient_ssh_error(exception) is expected


class _FakeTransport:
    """Fake paramiko transport for testing."""

    def __init__(self, *, is_active: bool = True) -> None:
        self._is_active = is_active

    def is_active(self) -> bool:
        return self._is_active


class _BaseFakeSFTP:
    """Base class for fake SFTP clients used in tests."""

    def close(self) -> None:
        pass


class _FakeSSHClient:
    """Minimal fake paramiko SSHClient for testing the paramiko upload path."""

    def __init__(self, transport_return: object = None) -> None:
        self._transport = transport_return
        self.close_call_count = 0

    def get_transport(self) -> object:
        return self._transport

    def close(self) -> None:
        self.close_call_count += 1


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
        run_shell_command_results: list[tuple[bool, CommandOutput] | Exception] | None = None,
    ) -> None:
        super().__init__(
            get_file_results=get_file_results,
            put_file_results=put_file_results,
            run_shell_command_results=run_shell_command_results,
        )
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
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
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


def test_get_file_timeout_error_disconnects_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """TimeoutError (pyinfra/paramiko read timeout) should disconnect before retrying.

    ``TimeoutError`` is an ``OSError`` subclass on Python 3, so the inner
    retry handler must branch on it BEFORE the generic OSError branch --
    otherwise the file-not-found / socket-closed string-matches would run
    against the timeout exception, miss, and re-raise without disconnect,
    leaving the retry to reuse the same dead SSH channel.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Timed out reading output")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 1


def test_put_file_timeout_error_disconnects_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """TimeoutError (pyinfra/paramiko write timeout) should disconnect before retrying.

    Parallel to ``test_get_file_timeout_error_disconnects_before_retry``;
    see that docstring for the ordering rationale.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Timed out writing output")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 1


def test_get_file_wraps_timeout_error_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """After retries are exhausted, TimeoutError must be wrapped in HostConnectionError.

    Without an explicit TimeoutError branch in ``_get_file``'s outer
    handler, the post-retry timeout would fall into the generic OSError
    branch and re-raise as raw ``OSError`` (since "Socket is closed"
    won't match a timeout message), leaking the underlying class to
    callers.
    """

    class _HostWithImmediateTimeout(Host):
        def _get_file_with_transient_retry(
            self,
            remote_filename: str,
            filename_or_io: str | IO[bytes],
            remote_temp_filename: str | None = None,
        ) -> bool:
            raise TimeoutError("Timed out reading output")

    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=_FakeTransport()))
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateTimeout(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    with pytest.raises(HostConnectionError, match="timed out while reading file"):
        host._get_file("/remote/file.txt", io.BytesIO())


def test_put_file_wraps_timeout_error_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """After retries are exhausted, TimeoutError must be wrapped in HostConnectionError.

    Parallel to ``test_get_file_wraps_timeout_error_in_host_connection_error``.
    """

    class _HostWithImmediateTimeout(Host):
        def _put_file_with_transient_retry(
            self,
            filename_or_io: str | IO[str] | IO[bytes],
            remote_filename: str,
            remote_temp_filename: str | None = None,
        ) -> bool:
            raise TimeoutError("Timed out writing output")

    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=_FakeTransport()))
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateTimeout(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    with pytest.raises(HostConnectionError, match="timed out while writing file"):
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
            remote_temp_filename: str | None = None,
        ) -> bool:
            raise SSHException("connection lost")

    fake = _FakeHostWithSSH(ssh_client=_FakeSSHClient(transport_return=_FakeTransport()))
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateSSHFailure(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    with pytest.raises(HostConnectionError, match="Could not read file"):
        host._get_file("/remote/file.txt", io.BytesIO())


def test_get_file_channel_closed_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """SSHException("Channel closed.") should retry without calling disconnect.

    "Channel closed" means a specific channel died, not the whole transport.
    Disconnecting would kill other threads' in-flight operations.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def getfo(self, remote_path: str, fl: IO[bytes]) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SSHException("Channel closed.")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._get_file("/remote/file.txt", io.BytesIO())

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 0


def test_put_file_channel_closed_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """SSHException("Channel closed.") should retry without calling disconnect.

    "Channel closed" means a specific channel died, not the whole transport.
    Disconnecting would kill other threads' in-flight operations.
    """
    call_count = 0

    class _FailOnceThenSucceedSFTP(_BaseFakeSFTP):
        def putfo(self, fl: IO[bytes], remote_path: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise SSHException("Channel closed.")

    host, fake = _create_host_with_custom_sftp_and_fake(local_provider, _FailOnceThenSucceedSFTP)
    result = host._put_file(io.BytesIO(b"content"), "/remote/file.txt")

    assert result is True
    assert call_count == 2
    assert fake.disconnect_call_count == 0


@pytest.mark.parametrize(
    "exception",
    [OSError("Socket is closed"), SSHException("SSH session not active"), EOFError()],
    ids=["socket-closed", "ssh-exception", "eof-error"],
)
def test_run_shell_command_retries_on_transient_error(
    local_provider: LocalProviderInstance,
    exception: Exception,
) -> None:
    """Transient SSH errors should be transparently retried on _run_shell_command."""
    ok_result = (True, CommandOutput([]))
    fake = _FakePyinfraHost(run_shell_command_results=[exception, ok_result])
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("echo hello"))

    assert success is True
    assert fake._run_shell_command_call_count == 2


def test_run_shell_command_channel_exception_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """ChannelException should retry without calling disconnect on the connector."""
    ok_result = (True, CommandOutput([]))
    fake = _FakePyinfraHost(run_shell_command_results=[ChannelException(2, "open failed"), ok_result])
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("echo hello"))

    assert success is True
    assert fake._run_shell_command_call_count == 2
    assert fake.disconnect_call_count == 0


def test_run_shell_command_channel_closed_retries_without_disconnect(
    local_provider: LocalProviderInstance,
) -> None:
    """SSHException("Channel closed.") should retry without calling disconnect.

    "Channel closed" means a specific channel died, not the whole transport.
    Disconnecting would kill other threads' in-flight operations.
    """
    ok_result = (True, CommandOutput([]))
    fake = _FakePyinfraHost(run_shell_command_results=[SSHException("Channel closed."), ok_result])
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("echo hello"))

    assert success is True
    assert fake._run_shell_command_call_count == 2
    assert fake.disconnect_call_count == 0


def test_run_shell_command_ssh_exception_disconnects_before_retry(
    local_provider: LocalProviderInstance,
) -> None:
    """Non-ChannelException, non-channel-closed SSHException should disconnect before retrying."""
    ok_result = (True, CommandOutput([]))
    fake = _FakePyinfraHost(run_shell_command_results=[SSHException("SSH session not active"), ok_result])
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("echo hello"))

    assert success is True
    assert fake._run_shell_command_call_count == 2
    assert fake.disconnect_call_count == 1


def test_run_shell_command_retries_on_dead_transport_ghost_failure(
    local_provider: LocalProviderInstance,
) -> None:
    """When run_shell_command returns (False, output) and the SSH transport is dead, retry.

    This happens when another thread disconnects the shared SSH connection:
    paramiko's recv_exit_status() returns -1 for the dead channel, and pyinfra
    returns (False, empty_output) without raising an exception.
    """
    dead_transport = _FakeTransport(is_active=False)
    ok_result = (True, CommandOutput([]))
    # First call: command "fails" (exit -1 from dead channel) with dead transport.
    # Second call: command succeeds after retry reconnects.
    ghost_failure = (False, CommandOutput([]))
    fake = _FakeHostWithSSH(
        ssh_client=_FakeSSHClient(transport_return=dead_transport),
        run_shell_command_results=[ghost_failure, ok_result],
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("mkdir -p /some/dir"))

    assert success is True
    assert fake._run_shell_command_call_count == 2
    assert fake.disconnect_call_count >= 1


def test_run_shell_command_does_not_retry_real_failure_with_live_transport(
    local_provider: LocalProviderInstance,
) -> None:
    """When run_shell_command returns (False, output) but the transport is alive, do not retry.

    This is a genuine command failure (e.g. permission denied), not a ghost failure.
    """
    live_transport = _FakeTransport(is_active=True)
    real_failure = (False, CommandOutput([]))
    fake = _FakeHostWithSSH(
        ssh_client=_FakeSSHClient(transport_return=live_transport),
        run_shell_command_results=[real_failure],
    )
    host = _create_host_with_fake_connector(local_provider, fake)

    success, _ = host._run_shell_command(StringCommand("false"))

    assert success is False
    assert fake._run_shell_command_call_count == 1
    assert fake.disconnect_call_count == 0


def test_run_shell_command_wraps_ssh_exception_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """After all retries are exhausted, SSHException should be wrapped in HostConnectionError."""

    class _HostWithImmediateSSHFailure(Host):
        def _run_shell_command_with_transient_retry(
            self,
            command: StringCommand,
            pyinfra_kwargs: dict[str, Any],
        ) -> tuple[bool, CommandOutput]:
            raise SSHException("connection lost")

    fake = _FakePyinfraHost()
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateSSHFailure(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    with pytest.raises(HostConnectionError, match="Could not execute command"):
        host._run_shell_command(StringCommand("echo hello"))


def test_run_shell_command_wraps_timeout_error_in_host_connection_error(
    local_provider: LocalProviderInstance,
) -> None:
    """After all retries are exhausted, TimeoutError must be wrapped in HostConnectionError.

    ``TimeoutError`` is an ``OSError`` subclass on Python 3, so the outer
    handler's ordering matters: the TimeoutError branch must precede the
    narrow "Socket is closed" OSError check, otherwise post-retry timeouts
    leak to callers as raw ``OSError`` rather than the structured
    ``HostConnectionError`` wrapper.
    """

    class _HostWithImmediateTimeout(Host):
        def _run_shell_command_with_transient_retry(
            self,
            command: StringCommand,
            pyinfra_kwargs: dict[str, Any],
        ) -> tuple[bool, CommandOutput]:
            raise TimeoutError("Timed out reading output")

    fake = _FakePyinfraHost()
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = _HostWithImmediateTimeout(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    with pytest.raises(HostConnectionError, match="timed out reading output"):
        host._run_shell_command(StringCommand("echo hello"))


# =========================================================================
# Tests for disconnect / _close_paramiko_client
# =========================================================================


def test_disconnect_closes_paramiko_client(
    local_provider: LocalProviderInstance,
) -> None:
    """Host.disconnect() must call close() on the underlying paramiko SSH client.

    pyinfra's disconnect() only clears its SFTP cache and sets connected=False.
    It does not close the paramiko SSHClient, which would leak the TCP socket.
    """
    ssh_client = _FakeSSHClient(transport_return=_FakeTransport())
    fake = _FakeHostWithSSH(ssh_client=ssh_client)
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = Host(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    host.disconnect()

    assert ssh_client.close_call_count == 1


def test_disconnect_is_safe_without_paramiko_client(
    local_provider: LocalProviderInstance,
) -> None:
    """Host.disconnect() must not raise when the pyinfra host has no SSH client."""
    fake = _FakeHostWithSSH(ssh_client=None)
    connector = PyinfraConnector(cast(PyinfraHost, fake))
    host = Host(
        id=HostId.generate(),
        host_name=HostName("test"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=local_provider.mngr_ctx,
    )

    # Should not raise
    host.disconnect()


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
# Tests for Host.get_name()
# =========================================================================


def test_host_get_connector_host_name_strips_at_prefix_for_local_host(
    local_host: Host,
) -> None:
    """get_connector_host_name() should strip pyinfra's internal '@' prefix from local host names."""
    assert local_host.get_connector_host_name() == HostName("local")


def test_host_get_name_returns_host_name(
    local_host: Host,
) -> None:
    """get_name() returns the explicit host_name supplied at Host construction.

    Local provider hosts construct Host with host_name=LOCAL_HOST_NAME
    ("localhost") rather than relying on the pyinfra connector's "@local"
    string.
    """
    assert local_host.get_name() == HostName(LOCAL_HOST_NAME)


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
    assert data.host_name == LOCAL_HOST_NAME


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


def test_build_remote_lock_command_blocking_holds_flock_until_stdin_closes() -> None:
    """The blocking remote lock command must be valid shell, flock the path, and signal acquisition."""
    cmd = _build_remote_lock_command(Path("/mngr/host_lock"), timeout_seconds=None)
    assert subprocess.run(["sh", "-n", "-c", cmd], capture_output=True).returncode == 0
    assert "flock 9" in cmd
    assert "/mngr/host_lock" in cmd
    assert _LOCK_ACQUIRED_MARKER in cmd
    # It must wait on stdin (so closing the channel releases the lock).
    assert "read" in cmd


def test_build_remote_lock_command_with_timeout_uses_flock_wait() -> None:
    """A bounded remote lock command must use ``flock -w`` and emit the timeout marker."""
    cmd = _build_remote_lock_command(Path("/mngr/host_lock"), timeout_seconds=12.0)
    assert subprocess.run(["sh", "-n", "-c", cmd], capture_output=True).returncode == 0
    assert "flock -w 12 9" in cmd
    assert _LOCK_ACQUIRED_MARKER in cmd
    assert _LOCK_TIMED_OUT_MARKER in cmd


def test_host_lock_cooperatively_acquires_and_releases_blocking(
    local_host: Host,
) -> None:
    """lock_cooperatively with an indefinite timeout should acquire and release the lock."""
    host = local_host
    with host.lock_cooperatively(timeout_seconds=None):
        assert host.is_lock_held() is True
    # The lock file persists (stable inode) but is no longer held.
    assert (host.host_dir / "host_lock").exists()
    assert host.is_lock_held() is False


def test_host_lock_cooperatively_is_mutually_exclusive(
    local_host: Host,
) -> None:
    """A second lock_cooperatively must block until the first releases (real flock)."""
    host = local_host
    first_acquired = threading.Event()
    allow_first_release = threading.Event()
    second_acquired = threading.Event()

    def hold_first() -> None:
        with host.lock_cooperatively(timeout_seconds=None):
            first_acquired.set()
            # Hold the lock until the test signals release.
            allow_first_release.wait(timeout=30.0)

    def acquire_second() -> None:
        # Only attempt once the first holder is established.
        first_acquired.wait(timeout=30.0)
        with host.lock_cooperatively(timeout_seconds=None):
            second_acquired.set()

    first_thread = threading.Thread(target=hold_first)
    second_thread = threading.Thread(target=acquire_second)
    first_thread.start()
    second_thread.start()
    try:
        assert first_acquired.wait(timeout=30.0)
        # The second acquisition must not succeed while the first holds the lock.
        assert not second_acquired.wait(timeout=1.0)
        # Releasing the first lets the second proceed.
        allow_first_release.set()
        assert second_acquired.wait(timeout=30.0)
    finally:
        allow_first_release.set()
        first_thread.join(timeout=30.0)
        second_thread.join(timeout=30.0)


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


@pytest.mark.allow_warnings(match=r"^Agent .* has type .* which is no longer registered")
def test_host_get_agents_tolerates_agent_with_unregistered_type(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """get_agents must not blow up when an on-disk agent's type is no longer registered.

    Simulates the "plugin was uninstalled after the agent was created" case:
    write an agent state directory whose data.json names a type that does not
    appear in any registry or in user config. Loading should degrade to
    BaseAgent and produce an entry instead of raising UnknownAgentTypeError.
    Without this tolerance, every mngr command that lists agents (destroy,
    cleanup, gc, list, ...) would break for affected users.
    """
    host = local_host
    # Plant a data.json by hand so we control the type string without going
    # through host.create_agent_state (which validates the type).
    agent_id = AgentId.generate()
    agent_dir = temp_host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(
        json.dumps(
            {
                "id": str(agent_id),
                "name": "orphan-agent",
                "type": "uninstalled-plugin-type",
                "work_dir": str(temp_work_dir),
                "create_time": "2026-01-01T00:00:00+00:00",
            }
        )
    )

    agents = host.get_agents()
    agents_by_name = {str(a.name): a for a in agents}
    assert "orphan-agent" in agents_by_name
    # The docstring promises we degrade to BaseAgent; pin that explicitly so a
    # regression that returns the wrong fallback class would be caught here.
    assert isinstance(agents_by_name["orphan-agent"], BaseAgent)


# =========================================================================
# Tests for Host.provision_agent
# =========================================================================


def test_host_provision_agent_basic(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """provision_agent should run through basic provisioning without errors."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("provision-test-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )

    agent = host.create_agent_state(temp_work_dir, options)
    host.provision_agent(agent, options, temp_mngr_ctx)

    # Verify env file was created with MNGR-specific variables
    env_path = temp_host_dir / "agents" / str(agent.id) / "env"
    assert env_path.exists()
    env_content = env_path.read_text()
    assert "MNGR_AGENT_ID" in env_content
    assert "MNGR_AGENT_NAME" in env_content
    assert "MNGR_AGENT_WORK_DIR" in env_content
    assert "MNGR_HOST_DIR" in env_content


def test_host_provision_agent_with_env_vars(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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
    host.provision_agent(agent, options, temp_mngr_ctx)

    # Verify custom env vars are in the env file
    env_path = temp_host_dir / "agents" / str(agent.id) / "env"
    env_content = env_path.read_text()
    assert "CUSTOM_KEY=custom_value" in env_content
    assert "DEBUG=true" in env_content


def test_host_provision_agent_with_extra_provision_commands(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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
    host.provision_agent(agent, options, temp_mngr_ctx)

    assert marker_file.exists()
    assert "provisioned" in marker_file.read_text()


def test_host_provision_agent_with_create_directories(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
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
    host.provision_agent(agent, options, temp_mngr_ctx)

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
    assert "C-q" in content
    assert "C-t" in content


def test_host_create_host_tmux_config_contains_mngr_settings(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """The config sets mngr's status-left-length and set-titles options."""
    host = local_host
    content = host._create_host_tmux_config().read_text()

    assert f"set -g status-left-length {_TMUX_STATUS_LEFT_LENGTH}" in content
    assert "set -g set-titles on" in content
    assert f'set -g set-titles-string "{_TMUX_SET_TITLES_STRING}"' in content


def test_host_create_host_tmux_config_does_not_source_user_config(
    local_host: Host,
    temp_host_dir: Path,
) -> None:
    """The generated config must not source the user's ~/.tmux.conf.

    tmux loads ~/.tmux.conf itself when the server starts; re-sourcing it from
    mngr's config on every agent creation would re-run non-idempotent user
    config (e.g. 'set -ag', plugin 'run-shell') and corrupt their setup.
    """
    host = local_host
    content = host._create_host_tmux_config().read_text()

    assert "~/.tmux.conf" not in content
    assert "source-file" not in content


def _make_local_host_with_tmux_config(
    tmux_config: TmuxConfig,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    active_concurrency_group: ConcurrencyGroup,
    mngr_test_prefix: str,
) -> Host:
    """Build a local Host whose MngrConfig carries the given tmux config."""
    config = MngrConfig(
        default_host_dir=temp_host_dir,
        prefix=mngr_test_prefix,
        is_error_reporting_enabled=False,
        tmux=tmux_config,
    )
    ctx = make_mngr_ctx(config, plugin_manager, temp_profile_dir, concurrency_group=active_concurrency_group)
    provider = LocalProviderInstance(name=ProviderInstanceName("local"), host_dir=temp_host_dir, mngr_ctx=ctx)
    return provider.create_host(HostName(LOCAL_HOST_NAME))


def test_host_create_host_tmux_config_sources_additional_config_when_configured(
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    active_concurrency_group: ConcurrencyGroup,
    mngr_test_prefix: str,
) -> None:
    """A configured tmux.additional_config_path is sourced (guarded by test -f) into the session config."""
    additional_config_path = "~/.mngr/tmux.user.conf"
    host = _make_local_host_with_tmux_config(
        TmuxConfig(additional_config_path=Path(additional_config_path)),
        temp_host_dir,
        temp_profile_dir,
        plugin_manager,
        active_concurrency_group,
        mngr_test_prefix,
    )
    content = host._create_host_tmux_config().read_text()

    assert f"if-shell 'test -f {additional_config_path}' 'source-file {additional_config_path}'" in content
    # Only the configured additional_config_path is sourced; mngr never sources ~/.tmux.conf
    # (tmux loads that itself at server start).
    assert content.count("source-file") == 1
    assert "~/.tmux.conf" not in content
    # The path is interpolated unquoted so a leading ~ is expanded by the host shell/tmux.
    assert f"'{additional_config_path}'" not in content


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
    assert "MNGR_SAVED_DEFAULT_TMUX_COMMAND" in result


# =========================================================================
# Tests for Host._collect_agent_env_vars
# =========================================================================


def test_host_collect_agent_env_vars_includes_mngr_variables(
    local_host: Host,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """_collect_agent_env_vars should include MNGR-specific variables."""
    host = local_host
    options = CreateAgentOptions(
        name=AgentName("env-collect-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    env = host._collect_agent_env_vars(agent, options)
    assert env["MNGR_HOST_DIR"] == str(temp_host_dir)
    assert env["MNGR_AGENT_ID"] == str(agent.id)
    assert env["MNGR_AGENT_NAME"] == str(agent.name)
    assert env["MNGR_AGENT_WORK_DIR"] == str(temp_work_dir)
    assert "MNGR_AGENT_STATE_DIR" in env
    assert "LLM_USER_PATH" in env
    assert "MNGR_GIT_BASE_BRANCH" in env
    # CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH is a parallel export the
    # imbue-code-guardian plugin's stop hook reads; it must always be set
    # alongside MNGR_GIT_BASE_BRANCH and must hold the same value.
    assert "CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH" in env
    assert env["CODE_GUARDIAN_STOP_HOOK__BASE_BRANCH"] == env["MNGR_GIT_BASE_BRANCH"]
    # The primary tmux window name is exported (default "agent") so in-session
    # helpers like the ttyd attach script can target the window by name, not :0.
    assert env["MNGR_PRIMARY_WINDOW_NAME"] == "agent"


def test_host_collect_agent_env_vars_uses_configured_primary_window_name(
    temp_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    active_concurrency_group: ConcurrencyGroup,
    mngr_test_prefix: str,
) -> None:
    """A custom tmux.primary_window_name flows into MNGR_PRIMARY_WINDOW_NAME."""
    host = _make_local_host_with_tmux_config(
        TmuxConfig(primary_window_name="primary"),
        temp_host_dir,
        temp_profile_dir,
        plugin_manager,
        active_concurrency_group,
        mngr_test_prefix,
    )
    options = CreateAgentOptions(
        name=AgentName("env-window-agent"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    agent = host.create_agent_state(temp_work_dir, options)

    env = host._collect_agent_env_vars(agent, options)
    assert env["MNGR_PRIMARY_WINDOW_NAME"] == "primary"


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


# =========================================================================
# Tests for Host._apply_work_dir_extra_paths
# =========================================================================


def test_apply_work_dir_extra_paths_share_same_host_creates_symlink(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Share mode on same host should create a symlink."""
    source_dir, work_dir = source_and_work_dirs
    (source_dir / ".venv").mkdir()

    local_host._apply_work_dir_extra_paths(local_host, source_dir, work_dir, {".venv": WorkDirExtraPathMode.SHARE})

    target = work_dir / ".venv"
    assert target.is_symlink()
    assert target.resolve() == (source_dir / ".venv").resolve()


@pytest.mark.allow_warnings(match=r"^work_dir_extra_paths: source path does not exist, skipping")
def test_apply_work_dir_extra_paths_share_same_host_source_missing_warns(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Share mode should warn and skip when source path does not exist."""
    source_dir, work_dir = source_and_work_dirs
    # Do NOT create .venv

    local_host._apply_work_dir_extra_paths(local_host, source_dir, work_dir, {".venv": WorkDirExtraPathMode.SHARE})

    target = work_dir / ".venv"
    assert not target.exists()


def test_apply_work_dir_extra_paths_share_same_host_idempotent(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Share mode should skip if correct symlink already exists."""
    source_dir, work_dir = source_and_work_dirs
    (source_dir / ".venv").mkdir()

    # Create correct symlink first
    (work_dir / ".venv").symlink_to(source_dir / ".venv")

    # Should not raise
    local_host._apply_work_dir_extra_paths(local_host, source_dir, work_dir, {".venv": WorkDirExtraPathMode.SHARE})

    assert (work_dir / ".venv").is_symlink()


def test_apply_work_dir_extra_paths_share_same_host_existing_non_symlink_raises(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Share mode should raise if a non-symlink target already exists."""
    source_dir, work_dir = source_and_work_dirs
    (source_dir / ".venv").mkdir()
    # Create a real directory (not a symlink) at the target location
    (work_dir / ".venv").mkdir()

    with pytest.raises(UserInputError, match="not a symlink"):
        local_host._apply_work_dir_extra_paths(local_host, source_dir, work_dir, {".venv": WorkDirExtraPathMode.SHARE})


@pytest.mark.rsync
@pytest.mark.flaky
def test_apply_work_dir_extra_paths_copy_mode_copies_files(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Copy mode should copy files (not symlink) even on same host."""
    source_dir, work_dir = source_and_work_dirs
    test_output = source_dir / ".test_output"
    test_output.mkdir()
    (test_output / "results.txt").write_text("test results")

    local_host._apply_work_dir_extra_paths(
        local_host, source_dir, work_dir, {".test_output": WorkDirExtraPathMode.COPY}
    )

    target = work_dir / ".test_output"
    assert target.exists()
    assert not target.is_symlink()
    assert (target / "results.txt").read_text() == "test results"


def test_apply_work_dir_extra_paths_rejects_absolute_path(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Absolute paths should be rejected."""
    source_dir, work_dir = source_and_work_dirs

    with pytest.raises(UserInputError, match="absolute paths"):
        local_host._apply_work_dir_extra_paths(
            local_host, source_dir, work_dir, {"/etc/passwd": WorkDirExtraPathMode.COPY}
        )


def test_apply_work_dir_extra_paths_rejects_path_escaping_root(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Paths that escape the project root should be rejected."""
    source_dir, work_dir = source_and_work_dirs

    with pytest.raises(UserInputError, match="escapes project root"):
        local_host._apply_work_dir_extra_paths(
            local_host, source_dir, work_dir, {"../escape": WorkDirExtraPathMode.COPY}
        )


def test_apply_work_dir_extra_paths_nested_path_creates_parents(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
) -> None:
    """Nested paths should have parent directories created."""
    source_dir, work_dir = source_and_work_dirs
    nested = source_dir / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("content")

    local_host._apply_work_dir_extra_paths(
        local_host, source_dir, work_dir, {"deep/nested": WorkDirExtraPathMode.SHARE}
    )

    target = work_dir / "deep" / "nested"
    assert target.is_symlink()
    assert target.resolve() == nested.resolve()


def test_apply_work_dir_extra_paths_share_same_host_replaces_stale_symlink(
    local_host: Host,
    source_and_work_dirs: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """Share mode should replace a symlink that points to the wrong target."""
    source_dir, work_dir = source_and_work_dirs
    (source_dir / ".venv").mkdir()

    # Create a stale symlink pointing to a different location
    stale_target = tmp_path / "old_venv"
    stale_target.mkdir()
    (work_dir / ".venv").symlink_to(stale_target)
    assert (work_dir / ".venv").resolve() == stale_target.resolve()

    local_host._apply_work_dir_extra_paths(local_host, source_dir, work_dir, {".venv": WorkDirExtraPathMode.SHARE})

    target = work_dir / ".venv"
    assert target.is_symlink()
    assert target.resolve() == (source_dir / ".venv").resolve()


def test_add_tags_syncs_to_certified_data(
    local_provider: LocalProviderInstance,
) -> None:
    """add_tags should persist tags to certified data so get_tags returns them."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    host.add_tags({"env": "staging", "team": "backend"})

    tags = host.get_tags()
    assert tags["env"] == "staging"
    assert tags["team"] == "backend"


def test_set_tags_syncs_to_certified_data(
    local_provider: LocalProviderInstance,
) -> None:
    """set_tags should replace all tags in certified data."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    host.add_tags({"old": "value"})
    host.set_tags({"new": "value"})

    tags = host.get_tags()
    assert tags == {"new": "value"}


def test_remove_tags_syncs_to_certified_data(
    local_provider: LocalProviderInstance,
) -> None:
    """remove_tags should remove keys from certified data."""
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)

    host.add_tags({"env": "staging", "team": "backend"})
    host.remove_tags(["env"])

    tags = host.get_tags()
    assert "env" not in tags
    assert tags["team"] == "backend"


# =============================================================================
# Tests for _merge_agent_type_provisioning
# =============================================================================


def test_merge_agent_type_provisioning_returns_unchanged_when_no_fields() -> None:
    """_merge_agent_type_provisioning should return the original options when agent config has no provisioning."""
    agent_config = AgentTypeConfig()
    options = CreateAgentOptions(agent_type=AgentTypeName("generic"))
    result = _merge_agent_type_provisioning(agent_config, options)
    assert result is options


def test_merge_agent_type_provisioning_prepends_extra_provision_commands() -> None:
    """Agent type extra_provision_command should be prepended before CLI commands."""
    agent_config = AgentTypeConfig(extra_provision_command=("echo agent_type",))
    options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        provisioning=AgentProvisioningOptions(extra_provision_commands=("echo cli",)),
    )
    result = _merge_agent_type_provisioning(agent_config, options)
    assert result.provisioning.extra_provision_commands == ("echo agent_type", "echo cli")


def test_merge_agent_type_provisioning_prepends_upload_files() -> None:
    """Agent type upload_file specs should be parsed and prepended."""
    agent_config = AgentTypeConfig(upload_file=("local.txt:/remote.txt",))
    options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        provisioning=AgentProvisioningOptions(
            upload_files=(UploadFileSpec(local_path=Path("cli.txt"), remote_path=Path("/cli.txt")),),
        ),
    )
    result = _merge_agent_type_provisioning(agent_config, options)
    assert len(result.provisioning.upload_files) == 2
    assert result.provisioning.upload_files[0].local_path == Path("local.txt")
    assert result.provisioning.upload_files[0].remote_path == Path("/remote.txt")
    assert result.provisioning.upload_files[1].local_path == Path("cli.txt")


def test_merge_agent_type_provisioning_prepends_env_vars() -> None:
    """Agent type env should be parsed and prepended to environment.env_vars."""
    agent_config = AgentTypeConfig(env=("AGENT_TYPE_VAR=1",))
    options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="CLI_VAR", value="2"),),
        ),
    )
    result = _merge_agent_type_provisioning(agent_config, options)
    assert len(result.environment.env_vars) == 2
    assert result.environment.env_vars[0].key == "AGENT_TYPE_VAR"
    assert result.environment.env_vars[0].value == "1"
    assert result.environment.env_vars[1].key == "CLI_VAR"


def test_merge_agent_type_provisioning_prepends_env_files() -> None:
    """Agent type env_file should be parsed and prepended to environment.env_files."""
    agent_config = AgentTypeConfig(env_file=("/etc/agent.env",))
    options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        environment=AgentEnvironmentOptions(
            env_files=(Path("/etc/cli.env"),),
        ),
    )
    result = _merge_agent_type_provisioning(agent_config, options)
    assert result.environment.env_files == (Path("/etc/agent.env"), Path("/etc/cli.env"))


def test_merge_agent_type_provisioning_prepends_create_directories() -> None:
    """Agent type create_directory should be parsed and prepended."""
    agent_config = AgentTypeConfig(create_directory=("/tmp/mydir",))
    options = CreateAgentOptions(
        agent_type=AgentTypeName("generic"),
        provisioning=AgentProvisioningOptions(create_directories=(Path("/tmp/existing"),)),
    )
    result = _merge_agent_type_provisioning(agent_config, options)
    assert result.provisioning.create_directories == (Path("/tmp/mydir"), Path("/tmp/existing"))


def test_merge_agent_type_provisioning_combines_provisioning_and_env() -> None:
    """Both provisioning and env fields should be merged in one call."""
    agent_config = AgentTypeConfig(
        extra_provision_command=("echo setup",),
        env=("KEY=val",),
    )
    options = CreateAgentOptions(agent_type=AgentTypeName("generic"))
    result = _merge_agent_type_provisioning(agent_config, options)
    assert result.provisioning.extra_provision_commands == ("echo setup",)
    assert result.environment.env_vars == (EnvVar(key="KEY", value="val"),)
