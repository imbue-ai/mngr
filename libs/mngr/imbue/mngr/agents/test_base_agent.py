"""Integration tests for the BaseAgent class."""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Mapping

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session


def _create_test_agent(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    agent_name: str,
    work_dir: Path,
    command: str = "sleep 100000",
    data_command: str | None = None,
) -> BaseAgent:
    """Helper function to create a test agent on the local provider.

    ``command`` is stored on ``agent_config.command`` (the assemble_command
    source). ``data_command`` is the value written into data.json's ``command``
    field, which is what ``get_command`` reads. They are kept as separate
    parameters so a test can pin which source a getter reads from: passing a
    ``data_command`` that differs from ``command`` proves that ``get_command``
    reads data.json rather than the config. When ``data_command`` is omitted it
    defaults to ``command`` for the common case.
    """
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    if data_command is None:
        data_command = command

    # Create agent directory structure
    agent_id = AgentId.generate()
    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Write basic data.json via the host interface to exercise the real write path.
    data = {
        "command": data_command,
        "start_on_boot": False,
    }
    host.write_file(agent_dir / "data.json", json.dumps(data, indent=2).encode())

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
        mngr_ctx=temp_mngr_ctx,
    )


def _reconstruct_agent(agent: BaseAgent, temp_mngr_ctx: MngrContext) -> BaseAgent:
    """Build a fresh BaseAgent pointing at the same on-disk state as ``agent``.

    Reading a value back through this independent instance (rather than the one
    that performed the write) proves the value was persisted to disk, not merely
    cached in the writer's memory.
    """
    return BaseAgent(
        id=agent.id,
        host_id=agent.host.id,
        name=agent.name,
        agent_type=agent.agent_type,
        agent_config=agent.agent_config,
        work_dir=agent.work_dir,
        create_time=agent.create_time,
        host=agent.host,
        mngr_ctx=temp_mngr_ctx,
    )


def test_base_agent_get_command(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test that get_command reads from data.json, not from agent_config.command.

    The data.json command and the config command are set to distinct values so
    this pins the source: a bug returning ``agent_config.command`` would yield
    "config-cmd" and fail.
    """
    agent = _create_test_agent(
        local_provider,
        temp_mngr_ctx,
        "test-cmd-agent",
        temp_work_dir,
        command="config-cmd",
        data_command="echo hello",
    )

    command = agent.get_command()

    assert command == CommandString("echo hello")


def test_base_agent_get_command_default_bash(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test that get_command returns 'bash' when no command is set."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-cmd", temp_work_dir)

    # Overwrite data.json with no command via the host interface (the documented
    # data path), rather than reaching into the private _get_data_path().
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    data_path = host.host_dir / "agents" / str(agent.id) / "data.json"
    host.write_file(data_path, json.dumps({}, indent=2).encode())

    command = agent.get_command()

    assert command == CommandString("bash")


def test_base_agent_get_labels_returns_empty_dict_by_default(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test getting labels returns empty dict when none are set."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-labels-empty", temp_work_dir)

    labels = agent.get_labels()

    assert isinstance(labels, dict)
    assert len(labels) == 0


def test_base_agent_set_labels(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test set_labels persists labels to disk (read back via a fresh agent).

    Reading through a freshly constructed BaseAgent (same id/host) proves the
    labels were written to data.json, not just cached on the writer instance.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-set-labels", temp_work_dir)

    agent.set_labels({"project": "mngr", "env": "staging"})

    reloaded = _reconstruct_agent(agent, temp_mngr_ctx)
    retrieved = reloaded.get_labels()
    assert len(retrieved) == 2
    assert retrieved["project"] == "mngr"
    assert retrieved["env"] == "staging"


def test_base_agent_set_labels_replaces_existing(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test that set_labels replaces all existing labels."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-replace-labels", temp_work_dir)

    agent.set_labels({"project": "mngr", "env": "staging"})
    agent.set_labels({"team": "infra"})

    retrieved = agent.get_labels()
    assert len(retrieved) == 1
    assert retrieved["team"] == "infra"
    assert "project" not in retrieved


def test_base_agent_get_is_start_on_boot(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test getting start_on_boot setting."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-boot", temp_work_dir)

    result = agent.get_is_start_on_boot()

    assert result is False


def test_base_agent_set_is_start_on_boot(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test setting start_on_boot setting.

    On-disk persistence of data.json fields is proved by
    test_base_agent_set_labels (fresh-instance read-back); this round-trips
    through the same instance to cover the boolean field specifically.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-set-boot", temp_work_dir)

    agent.set_is_start_on_boot(True)

    assert agent.get_is_start_on_boot() is True


@pytest.mark.tmux
def test_base_agent_is_running_false_when_no_tmux_session(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test is_running returns False when no tmux session exists (lifecycle state is STOPPED)."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-not-running", temp_work_dir)

    result = agent.is_running()

    assert result is False


@pytest.mark.tmux
def test_base_agent_get_lifecycle_state_stopped(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_lifecycle_state returns STOPPED when no tmux session."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-stopped", temp_work_dir)

    state = agent.get_lifecycle_state()

    assert state == AgentLifecycleState.STOPPED


class _HostRaisingConnectionError(Host):
    """A real local Host whose command execution always raises HostConnectionError.

    Used to exercise the branch of get_lifecycle_state() that maps a lost host
    connection to STOPPED. Subclasses the real Host (rather than mocking) so the
    rest of the agent's interaction with the host stays real.
    """

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise HostConnectionError("simulated connection loss")


def test_base_agent_get_lifecycle_state_stopped_on_host_connection_error(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """get_lifecycle_state returns STOPPED when the host raises HostConnectionError."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-conn-err", temp_work_dir)
    # Rebuild the agent on a host that fails every command with HostConnectionError.
    failing_host = _HostRaisingConnectionError.model_construct(**dict(agent.host))
    failing_agent = agent.model_copy_update(to_update(agent.field_ref().host, failing_host))

    state = failing_agent.get_lifecycle_state()

    assert state == AgentLifecycleState.STOPPED


@pytest.mark.tmux
def test_base_agent_running_state_and_is_running_when_session_active(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """A live tmux session running the expected process with an active file maps to RUNNING.

    This pins the RUNNING branch of get_lifecycle_state() and the True branch of
    is_running() (which is defined purely in terms of get_lifecycle_state). The
    no-tmux STOPPED path is covered separately above. Additional lifecycle
    branches (WAITING, DONE, REPLACED, RUNNING_UNKNOWN_AGENT_TYPE) are covered in
    base_agent_test.py.
    """
    # The expected_process_name derives from the data.json command basename, so
    # the tmux pane must run that same binary (sleep) for the state to be RUNNING.
    sleep_command = "sleep 738291"
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-running", temp_work_dir, command=sleep_command)
    session_name = agent.session_name

    agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' '{sleep_command}'",
        timeout_seconds=5.0,
    )
    # The active file in the agent's state dir distinguishes RUNNING from WAITING.
    active_file = agent.host.host_dir / "agents" / str(agent.id) / "active"
    agent.host.write_file(active_file, b"")

    try:
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.RUNNING,
            error_message="Expected agent lifecycle state to be RUNNING",
        )
        assert agent.is_running() is True
    finally:
        cleanup_tmux_session(session_name)


def test_base_agent_get_reported_url_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_url returns None when no URL file exists.

    The positive path (reads + strips the status/url file) is pinned by
    test_base_agent_get_reported_url_from_status_file below.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-url", temp_work_dir)

    url = agent.get_reported_url()

    assert url is None


def test_base_agent_get_reported_url_from_status_file(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """get_reported_url reads and strips the status/url file at the expected path."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-url", temp_work_dir)
    url_path = agent.host.host_dir / "agents" / str(agent.id) / "status" / "url"
    agent.host.write_file(url_path, b"http://localhost:8080\n")

    assert agent.get_reported_url() == "http://localhost:8080"


def test_base_agent_get_reported_start_time_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_start_time returns None when no start time file exists.

    The positive read path is exercised via runtime_seconds in
    test_base_agent_runtime_seconds_computed_from_reported_start_time, and
    directly in base_agent_test.py::test_get_reported_start_time_returns_datetime_when_set.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-start", temp_work_dir)

    start_time = agent.get_reported_start_time()

    assert start_time is None


def test_base_agent_get_reported_activity_time_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_activity_time returns None when no activity file exists.

    The positive read path is pinned by test_base_agent_record_activity, which
    records and reads back the USER activity time.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-activity", temp_work_dir)

    activity = agent.get_reported_activity_time(ActivitySource.USER)

    assert activity is None


def test_base_agent_record_activity(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test record_activity writes the recorded source's file with a fresh timestamp.

    Verifies the recorded time is close to now (not a wildly wrong value), that
    the write is isolated to the USER source (AGENT stays None), and that the
    JSON record contains the agent id and a millisecond-epoch ``time`` field.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-record-activity", temp_work_dir)

    before = datetime.now(timezone.utc)
    agent.record_activity(ActivitySource.USER)
    after = datetime.now(timezone.utc)

    activity_time = agent.get_reported_activity_time(ActivitySource.USER)
    assert activity_time is not None
    # The recorded time (file mtime) must fall within the call window (allow a
    # small filesystem mtime-resolution slack on either side).
    assert before - timedelta(seconds=2) <= activity_time <= after + timedelta(seconds=2)

    # Recording USER activity must not write the AGENT channel (per-source isolation).
    assert agent.get_reported_activity_time(ActivitySource.AGENT) is None

    # The JSON record holds debugging metadata; the time field is ms since epoch.
    record = agent.get_reported_activity_record(ActivitySource.USER)
    assert record is not None
    parsed = json.loads(record)
    assert parsed["agent_id"] == str(agent.id)
    recorded_dt = datetime.fromtimestamp(parsed["time"] / 1000, tz=timezone.utc)
    assert before - timedelta(seconds=2) <= recorded_dt <= after + timedelta(seconds=2)


def test_base_agent_get_reported_activity_record_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_reported_activity_record returns None when no activity file exists.

    The positive read path (parsing the recorded JSON) is pinned by
    test_base_agent_record_activity.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-activity-record", temp_work_dir)

    record = agent.get_reported_activity_record(ActivitySource.AGENT)

    assert record is None


def test_base_agent_get_plugin_data_empty(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_plugin_data returns empty dict when no plugin data exists.

    The positive path is pinned by test_base_agent_set_plugin_data.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-plugin", temp_work_dir)

    plugin_data = agent.get_plugin_data("test-plugin")

    assert plugin_data == {}


def test_base_agent_set_plugin_data(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test set_plugin_data stores plugin data.

    On-disk persistence of data.json fields is proved by
    test_base_agent_set_labels (fresh-instance read-back); this round-trips
    through the same instance to cover the plugin namespace specifically.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-set-plugin", temp_work_dir)

    agent.set_plugin_data("test-plugin", {"key": "value"})

    plugin_data = agent.get_plugin_data("test-plugin")
    assert plugin_data == {"key": "value"}


def test_base_agent_get_env_vars_empty(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_env_vars returns empty dict when no env file exists.

    The positive path is pinned by test_base_agent_set_env_vars.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-env", temp_work_dir)

    env = agent.get_env_vars()

    assert env == {}


def test_base_agent_set_env_vars(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test set_env_vars persists to the env file (read back via a fresh agent).

    Reading through a freshly constructed BaseAgent (same id/host) proves the env
    vars were written to the on-disk env file, not just cached on the writer.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-set-env", temp_work_dir)

    agent.set_env_vars({"MY_VAR": "my_value", "OTHER_VAR": "other_value"})

    reloaded = _reconstruct_agent(agent, temp_mngr_ctx)
    env = reloaded.get_env_vars()
    assert env["MY_VAR"] == "my_value"
    assert env["OTHER_VAR"] == "other_value"


def test_base_agent_get_env_var(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_env_var retrieves a single environment variable.

    On-disk persistence of the env file is proved by test_base_agent_set_env_vars
    (fresh-instance read-back); this covers single-key lookup including a miss.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-get-env-var", temp_work_dir)

    agent.set_env_vars({"TEST_VAR": "test_value"})

    value = agent.get_env_var("TEST_VAR")
    assert value == "test_value"

    missing = agent.get_env_var("MISSING_VAR")
    assert missing is None


def test_base_agent_set_env_var(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test set_env_var sets a single environment variable.

    On-disk persistence of the env file is proved by test_base_agent_set_env_vars
    (fresh-instance read-back); this covers the single-key setter path.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-set-env-var", temp_work_dir)

    agent.set_env_var("SINGLE_VAR", "single_value")

    value = agent.get_env_var("SINGLE_VAR")
    assert value == "single_value"


def test_base_agent_runtime_seconds_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test runtime_seconds is None when no start time reported."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-runtime", temp_work_dir)

    runtime = agent.runtime_seconds

    assert runtime is None


def test_base_agent_runtime_seconds_computed_from_reported_start_time(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """runtime_seconds is (now - reported start_time), exercising the subtraction.

    The early-return-None branch is covered above; this writes a start_time ~60s
    in the past (at the status/start_time path read by get_reported_start_time)
    and asserts the elapsed delta, pinning both the read path and the arithmetic.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-runtime", temp_work_dir)
    elapsed_seconds = 60
    start_time = datetime.now(timezone.utc) - timedelta(seconds=elapsed_seconds)
    status_path = agent.host.host_dir / "agents" / str(agent.id) / "status" / "start_time"
    agent.host.write_file(status_path, start_time.isoformat().encode())

    runtime = agent.runtime_seconds

    assert runtime is not None
    # A bug returning a constant or the wrong epoch would fail this magnitude check.
    assert abs(runtime - elapsed_seconds) < 5


def test_base_agent_get_initial_message_none(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_initial_message returns None when not set.

    The positive path is pinned by test_base_agent_get_initial_message_from_data.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-initial-msg", temp_work_dir)

    msg = agent.get_initial_message()

    assert msg is None


def test_base_agent_get_initial_message_from_data(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_initial_message returns value from data.json.

    Writes data.json through the host interface at the documented data path
    (host_dir/agents/<id>/data.json) rather than the private _get_data_path(),
    and asserts the parsed value to pin the path/parsing.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-with-initial-msg", temp_work_dir)

    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    data_path = host.host_dir / "agents" / str(agent.id) / "data.json"
    data = json.loads(host.read_text_file(data_path))
    data["initial_message"] = "Hello, agent!"
    host.write_file(data_path, json.dumps(data, indent=2).encode())

    msg = agent.get_initial_message()

    assert msg == "Hello, agent!"


def test_base_agent_assemble_command_from_override(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command uses command_override when provided."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-cmd-override", temp_work_dir)
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))

    command = agent.assemble_command(
        host=host,
        agent_args=(),
        command_override=CommandString("custom command"),
    )

    assert command == CommandString("custom command")


def test_base_agent_assemble_command_from_config(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command uses config command when no override."""
    agent = _create_test_agent(
        local_provider, temp_mngr_ctx, "test-cmd-config", temp_work_dir, command="config command"
    )
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))

    command = agent.assemble_command(
        host=host,
        agent_args=(),
        command_override=None,
    )

    assert command == CommandString("config command")


def test_base_agent_assemble_command_with_args(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command appends agent_args."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-cmd-args", temp_work_dir, command="base")
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))

    command = agent.assemble_command(
        host=host,
        agent_args=("--flag", "value"),
        command_override=None,
    )

    assert command == CommandString("base --flag value")


def test_base_agent_assemble_command_interleaves_cli_args_between_base_and_agent_args(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """assemble_command splices cli_args between the base command and agent_args.

    The expected order is base, then config cli_args, then agent_args. This pins
    the cli_args splice path that the other assemble_command tests never exercise
    (the default helper leaves cli_args unset).
    """
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
    agent_id = AgentId.generate()
    agent_dir = host.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    host.write_file(agent_dir / "data.json", json.dumps({"command": "base"}).encode())

    agent_config = AgentTypeConfig(
        command=CommandString("base"),
        cli_args=("--cli-one", "--cli-two"),
    )
    agent = BaseAgent(
        id=agent_id,
        host_id=host.id,
        name=AgentName("test-cli-args-splice"),
        agent_type=AgentTypeName("generic"),
        agent_config=agent_config,
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host=host,
        mngr_ctx=temp_mngr_ctx,
    )

    command = agent.assemble_command(
        host=host,
        agent_args=("--agent-arg",),
        command_override=None,
    )

    assert command == CommandString("base --cli-one --cli-two --agent-arg")


def test_base_agent_assemble_command_raises_when_no_base_and_no_args(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test assemble_command raises when neither config command nor agent_args provide a base."""
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))
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
        agent_type=AgentTypeName("generic"),
        agent_config=agent_config,
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host=host,
        mngr_ctx=temp_mngr_ctx,
    )

    with pytest.raises(UserInputError, match=r"has no command to run"):
        agent.assemble_command(host=host, agent_args=(), command_override=None)


def test_base_agent_list_reported_plugin_files_empty(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test list_reported_plugin_files returns empty list when no files.

    The positive path is pinned by
    test_base_agent_list_reported_plugin_files_returns_written_files below.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-no-plugin-files", temp_work_dir)

    files = agent.list_reported_plugin_files("test-plugin")

    assert files == []


def test_base_agent_list_reported_plugin_files_returns_written_files(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """list_reported_plugin_files lists files written under the plugin's reported dir."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-plugin-files", temp_work_dir)
    plugin_name = "test-plugin"
    agent.set_reported_plugin_file(plugin_name, "alpha.txt", "a")
    agent.set_reported_plugin_file(plugin_name, "beta.json", "{}")

    files = agent.list_reported_plugin_files(plugin_name)

    assert sorted(files) == ["alpha.txt", "beta.json"]


def test_base_agent_get_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """Test get_host returns the agent's host."""
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-get-host", temp_work_dir)

    host = agent.get_host()

    assert host is not None
    assert host.id is not None


def test_base_agent_get_provision_file_transfers_is_empty_by_default(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    temp_work_dir: Path,
) -> None:
    """The default get_provision_file_transfers declares no transfers.

    This is the one provisioning hook with an observable default return value.
    The sibling hooks (on_before_provisioning, provision, on_after_provisioning,
    on_destroy) are deliberate no-ops with empty bodies and no observable effect,
    so asserting they "don't raise" would guard nothing; they are not exercised
    here.
    """
    agent = _create_test_agent(local_provider, temp_mngr_ctx, "test-lifecycle", temp_work_dir)
    host = local_provider.get_host(HostName(LOCAL_HOST_NAME))

    options = CreateAgentOptions(
        name=AgentName("test"),
        agent_type=AgentTypeName("generic"),
    )

    transfers = agent.get_provision_file_transfers(host, options, temp_mngr_ctx)
    assert transfers == []
