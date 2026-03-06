"""Integration tests for the BaseAgent class."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import Permission
from imbue.mng.providers.local.instance import LocalProviderInstance


def _create_test_agent(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    agent_name: str,
    work_dir: Path,
    command: str = "sleep 100000",
) -> BaseAgent:
    """Helper function to create a test agent on the local provider."""
    host = local_provider.get_host(HostName("localhost"))

    # Create agent directory structure
    agent_id = AgentId.generate()
    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Write basic data.json
    data = {
        "command": command,
        "permissions": [],
        "start_on_boot": False,
    }
    (agent_dir / "data.json").write_text(json.dumps(data, indent=2))

    # Create agent config
    agent_config = AgentTypeConfig(
        command=CommandString(command),
    )

    return BaseAgent(
        id=agent_id,
        host_id=host.id,
        name=AgentName(agent_name),
        agent_type=AgentTypeName("generic"),
        agent_config=agent_config,
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host=host,
        mng_ctx=temp_mng_ctx,
    )


def test_base_agent_get_command(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that get_command returns the command from data.json."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-cmd-agent", temp_work_dir, command="echo hello")

    command = agent.get_command()

    assert command == CommandString("echo hello")


def test_base_agent_get_command_default_bash(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that get_command returns 'bash' when no command is set."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-cmd", temp_work_dir)

    # Write data.json without command
    data_path = agent._get_data_path()
    data_path.write_text(json.dumps({"permissions": []}, indent=2))

    command = agent.get_command()

    assert command == CommandString("bash")


def test_base_agent_get_permissions(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test getting permissions from agent."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-perms", temp_work_dir)

    perms = agent.get_permissions()

    assert isinstance(perms, list)
    assert len(perms) == 0


def test_base_agent_set_permissions(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test setting permissions on agent."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-perms", temp_work_dir)

    permissions = [Permission("read"), Permission("write")]
    agent.set_permissions(permissions)

    retrieved = agent.get_permissions()
    assert len(retrieved) == 2
    assert Permission("read") in retrieved
    assert Permission("write") in retrieved


def test_base_agent_get_labels_returns_empty_dict_by_default(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test getting labels returns empty dict when none are set."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-labels-empty", temp_work_dir)

    labels = agent.get_labels()

    assert isinstance(labels, dict)
    assert len(labels) == 0


def test_base_agent_set_labels(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test setting and retrieving labels on agent."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-labels", temp_work_dir)

    agent.set_labels({"project": "mng", "env": "staging"})

    retrieved = agent.get_labels()
    assert len(retrieved) == 2
    assert retrieved["project"] == "mng"
    assert retrieved["env"] == "staging"


def test_base_agent_set_labels_replaces_existing(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that set_labels replaces all existing labels."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-replace-labels", temp_work_dir)

    agent.set_labels({"project": "mng", "env": "staging"})
    agent.set_labels({"team": "infra"})

    retrieved = agent.get_labels()
    assert len(retrieved) == 1
    assert retrieved["team"] == "infra"
    assert "project" not in retrieved


def test_base_agent_get_is_start_on_boot(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test getting start_on_boot setting."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-boot", temp_work_dir)

    result = agent.get_is_start_on_boot()

    assert result is False


def test_base_agent_set_is_start_on_boot(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test setting start_on_boot setting."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-boot", temp_work_dir)

    agent.set_is_start_on_boot(True)

    assert agent.get_is_start_on_boot() is True


@pytest.mark.tmux
def test_base_agent_is_running_false_when_no_tmux_session(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test is_running returns False when no tmux session exists (lifecycle state is STOPPED)."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-not-running", temp_work_dir)

    result = agent.is_running()

    assert result is False


@pytest.mark.tmux
def test_base_agent_get_lifecycle_state_stopped(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
    mng_test_prefix: str,
) -> None:
    """Test get_lifecycle_state returns STOPPED when no tmux session."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-stopped", temp_work_dir)

    state = agent.get_lifecycle_state()

    assert state == AgentLifecycleState.STOPPED


def test_base_agent_get_reported_url_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_url returns None when no URL file exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-url", temp_work_dir)

    url = agent.get_reported_url()

    assert url is None


def test_base_agent_get_reported_start_time_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_start_time returns None when no start time file exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-start", temp_work_dir)

    start_time = agent.get_reported_start_time()

    assert start_time is None


def test_base_agent_get_reported_activity_time_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_activity_time returns None when no activity file exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-activity", temp_work_dir)

    activity = agent.get_reported_activity_time(ActivitySource.USER)

    assert activity is None


def test_base_agent_record_activity(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test record_activity creates activity file."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-record-activity", temp_work_dir)

    agent.record_activity(ActivitySource.USER)

    activity_time = agent.get_reported_activity_time(ActivitySource.USER)
    assert activity_time is not None
    assert isinstance(activity_time, datetime)


def test_base_agent_get_reported_activity_record_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_activity_record returns None when no activity file exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-activity-record", temp_work_dir)

    record = agent.get_reported_activity_record(ActivitySource.AGENT)

    assert record is None


def test_base_agent_get_plugin_data_empty(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_plugin_data returns empty dict when no plugin data exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-plugin", temp_work_dir)

    plugin_data = agent.get_plugin_data("test-plugin")

    assert plugin_data == {}


def test_base_agent_set_plugin_data(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test set_plugin_data stores plugin data."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-plugin", temp_work_dir)

    agent.set_plugin_data("test-plugin", {"key": "value"})

    plugin_data = agent.get_plugin_data("test-plugin")
    assert plugin_data == {"key": "value"}


def test_base_agent_get_env_vars_empty(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_env_vars returns empty dict when no env file exists."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-env", temp_work_dir)

    env = agent.get_env_vars()

    assert env == {}


def test_base_agent_set_env_vars(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test set_env_vars stores environment variables."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-env", temp_work_dir)

    agent.set_env_vars({"MY_VAR": "my_value", "OTHER_VAR": "other_value"})

    env = agent.get_env_vars()
    assert env["MY_VAR"] == "my_value"
    assert env["OTHER_VAR"] == "other_value"


def test_base_agent_get_env_var(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_env_var retrieves a single environment variable."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-get-env-var", temp_work_dir)

    agent.set_env_vars({"TEST_VAR": "test_value"})

    value = agent.get_env_var("TEST_VAR")
    assert value == "test_value"

    missing = agent.get_env_var("MISSING_VAR")
    assert missing is None


def test_base_agent_set_env_var(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test set_env_var sets a single environment variable."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-set-env-var", temp_work_dir)

    agent.set_env_var("SINGLE_VAR", "single_value")

    value = agent.get_env_var("SINGLE_VAR")
    assert value == "single_value"


def test_base_agent_runtime_seconds_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test runtime_seconds is None when no start time reported."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-runtime", temp_work_dir)

    runtime = agent.runtime_seconds

    assert runtime is None


def test_base_agent_get_initial_message_none(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_initial_message returns None when not set."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-initial-msg", temp_work_dir)

    msg = agent.get_initial_message()

    assert msg is None


def test_base_agent_get_initial_message_from_data(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_initial_message returns value from data.json."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-with-initial-msg", temp_work_dir)

    # Write initial message to data.json
    data_path = agent._get_data_path()
    data = json.loads(data_path.read_text())
    data["initial_message"] = "Hello, agent!"
    data_path.write_text(json.dumps(data, indent=2))

    msg = agent.get_initial_message()

    assert msg == "Hello, agent!"


def test_base_agent_assemble_command_from_override(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command uses command_override when provided."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-cmd-override", temp_work_dir)
    host = local_provider.get_host(HostName("localhost"))

    command = agent.assemble_command(
        host=host,
        agent_args=(),
        command_override=CommandString("custom command"),
    )

    assert command == CommandString("custom command")


def test_base_agent_assemble_command_from_config(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command uses config command when no override."""
    agent = _create_test_agent(
        local_provider, temp_mng_ctx, "test-cmd-config", temp_work_dir, command="config command"
    )
    host = local_provider.get_host(HostName("localhost"))

    command = agent.assemble_command(
        host=host,
        agent_args=(),
        command_override=None,
    )

    assert command == CommandString("config command")


def test_base_agent_assemble_command_with_args(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command appends agent_args."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-cmd-args", temp_work_dir, command="base")
    host = local_provider.get_host(HostName("localhost"))

    command = agent.assemble_command(
        host=host,
        agent_args=("--flag", "value"),
        command_override=None,
    )

    assert command == CommandString("base --flag value")


def test_base_agent_assemble_command_falls_back_to_agent_type(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command uses agent_type as command when no explicit command available.

    This verifies the documented "Direct command" fallback behavior where an unrecognized
    agent type is treated as a command to run.
    """
    host = local_provider.get_host(HostName("localhost"))
    host_id = host.id

    # Create agent with no command in config
    agent_id = AgentId.generate()
    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "data.json").write_text(json.dumps({}, indent=2))

    agent_config = AgentTypeConfig(
        command=None,
    )

    agent = BaseAgent(
        id=agent_id,
        host_id=host_id,
        name=AgentName("test-fallback-cmd"),
        agent_type=AgentTypeName("my-custom-type"),
        agent_config=agent_config,
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host=host,
        mng_ctx=temp_mng_ctx,
    )

    # When no command is defined, the agent type should be used as the command
    command = agent.assemble_command(host=host, agent_args=(), command_override=None)
    assert command == "my-custom-type"


def test_base_agent_list_reported_plugin_files_empty(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test list_reported_plugin_files returns empty list when no files."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-no-plugin-files", temp_work_dir)

    files = agent.list_reported_plugin_files("test-plugin")

    assert files == []


def test_base_agent_get_host(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test get_host returns the agent's host."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-get-host", temp_work_dir)

    host = agent.get_host()

    assert host is not None
    assert host.id is not None


def test_base_agent_get_command_basename(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test _get_command_basename extracts basename correctly."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-basename", temp_work_dir)

    # Test simple command
    assert agent._get_command_basename(CommandString("sleep 1000")) == "sleep"

    # Test with path
    assert agent._get_command_basename(CommandString("/usr/bin/sleep 1000")) == "sleep"

    # Test command with arguments
    assert agent._get_command_basename(CommandString("python script.py --flag")) == "python"


def test_base_agent_lifecycle_hooks_are_noop(
    local_provider: LocalProviderInstance,
    temp_mng_ctx: MngContext,
    temp_work_dir: Path,
) -> None:
    """Test that default lifecycle hooks are no-ops."""
    agent = _create_test_agent(local_provider, temp_mng_ctx, "test-lifecycle", temp_work_dir)
    host = local_provider.get_host(HostName("localhost"))

    options = CreateAgentOptions(
        name=AgentName("test"),
        agent_type=AgentTypeName("generic"),
    )

    # These should not raise
    agent.on_before_provisioning(host, options, temp_mng_ctx)
    agent.provision(host, options, temp_mng_ctx)
    agent.on_after_provisioning(host, options, temp_mng_ctx)

    # get_provision_file_transfers should return empty list
    transfers = agent.get_provision_file_transfers(host, options, temp_mng_ctx)
    assert transfers == []
