"""Tests for BaseAgent lifecycle state detection and data methods."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.host import DEFAULT_AGENT_READY_TIMEOUT_SECONDS
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import Permission
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.testing.testing import cleanup_tmux_session
from imbue.mng.testing.testing import get_short_random_string
from imbue.mng.utils.polling import wait_for


def create_test_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
    agent_config: AgentTypeConfig | None = None,
    agent_type: AgentTypeName | None = None,
) -> BaseAgent:
    """Create a test agent backed by a real local host filesystem.

    Accepts optional agent_config and agent_type overrides for tests that
    need non-default configuration (e.g., assemble_command tests).
    """
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent_id = AgentId.generate()
    agent_name = AgentName(f"test-agent-{get_short_random_string()}")
    resolved_type = agent_type or AgentTypeName("test")
    resolved_config = agent_config or AgentTypeConfig(command=CommandString("sleep 1000"))

    agent_dir = local_provider.host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)

    data: dict = {
        "id": str(agent_id),
        "name": str(agent_name),
        "type": str(resolved_type),
        "work_dir": str(temp_work_dir),
        "create_time": datetime.now(timezone.utc).isoformat(),
        "start_on_boot": False,
    }
    if resolved_config.command is not None:
        data["command"] = str(resolved_config.command)
    data_path = agent_dir / "data.json"
    data_path.write_text(json.dumps(data, indent=2))

    return BaseAgent(
        id=agent_id,
        name=agent_name,
        agent_type=resolved_type,
        work_dir=temp_work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        host=host,
        mng_ctx=local_provider.mng_ctx,
        agent_config=resolved_config,
    )


@pytest.fixture
def test_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> BaseAgent:
    return create_test_agent(local_provider, temp_host_dir, temp_work_dir)


@pytest.mark.tmux
def test_lifecycle_state_stopped_when_no_tmux_session(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is STOPPED when there is no tmux session."""
    state = test_agent.get_lifecycle_state()
    assert state == AgentLifecycleState.STOPPED


def _create_running_agent(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
    # unique sleep duration to avoid collisions with other tests
    sleep_duration: int,
) -> tuple[BaseAgent, str]:
    """Create an agent with a running tmux session and active file.

    Returns the agent and its tmux session name. Caller must clean up
    the session (e.g. with cleanup_tmux_session).
    """
    test_agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir)
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'sleep {sleep_duration}'",
        timeout_seconds=5.0,
    )

    # Create the active file in the agent's state directory (signals RUNNING)
    agent_dir = local_provider.host_dir / "agents" / str(test_agent.id)
    active_file = agent_dir / "active"
    active_file.write_text("")

    return test_agent, session_name


@pytest.mark.tmux
def test_lifecycle_state_running_when_expected_process_exists(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that agent is RUNNING when tmux session exists with expected process and active file."""
    test_agent, session_name = _create_running_agent(local_provider, temp_host_dir, temp_work_dir, 847291)

    try:
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.RUNNING,
            error_message="Expected agent lifecycle state to be RUNNING",
        )
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_is_running_true_when_tmux_session_running(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that is_running returns True when tmux session exists with expected process and active file."""
    test_agent, session_name = _create_running_agent(local_provider, temp_host_dir, temp_work_dir, 847293)

    try:
        wait_for(
            lambda: test_agent.is_running(),
            error_message="Expected is_running() to return True for running agent",
        )
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_replaced_when_different_process_exists(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is REPLACED when tmux session exists with different process."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session with a different command (cat waits for input indefinitely)
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'cat'",
        timeout_seconds=5.0,
    )

    try:
        # Poll for up to 5 seconds for the state to become REPLACED
        # There's a race condition where tmux spawns a shell first, then execs the command.
        # During that brief window, pane_current_command shows the shell, giving DONE.
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.REPLACED,
            error_message="Expected agent lifecycle state to be REPLACED",
        )
    finally:
        # Clean up tmux session and all its processes
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_done_when_no_process_in_pane(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is DONE when tmux session exists but no process is running."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session, then manually stop the process inside it
    # First create it with a long-running command
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}'",
        timeout_seconds=5.0,
    )

    # The tmux session now has a shell with no child processes (DONE state)
    try:
        # Poll for up to 5 seconds for the state to become DONE
        # There's a race condition where tmux may have brief child processes during init
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.DONE,
            error_message="Expected agent lifecycle state to be DONE",
        )
    finally:
        # Clean up tmux session and all its processes
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_waiting_when_no_active_file(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is WAITING when tmux session exists with expected process but no active file."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'sleep 1000'",
        timeout_seconds=5.0,
    )

    # No active file is created, so agent should be WAITING

    try:
        # Poll for up to 5 seconds for the state to become WAITING
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            error_message="Expected agent lifecycle state to be WAITING",
        )
    finally:
        # Clean up tmux session and all its processes
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_running_when_active_file_created(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that agent transitions from WAITING to RUNNING when active file is created."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'sleep 1000'",
        timeout_seconds=5.0,
    )

    agent_dir = local_provider.host_dir / "agents" / str(test_agent.id)

    try:
        # First verify it's in WAITING state (no active file)
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            error_message="Expected agent lifecycle state to be WAITING",
        )

        # Create the active file
        active_file = agent_dir / "active"
        active_file.write_text("")

        # Now verify it's in RUNNING state
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.RUNNING,
            error_message="Expected agent lifecycle state to be RUNNING after creating active file",
        )
    finally:
        # Clean up tmux session and all its processes
        cleanup_tmux_session(session_name)


def test_get_initial_message_returns_none_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_initial_message returns None when not set in data.json."""
    assert test_agent.get_initial_message() is None


def test_get_initial_message_returns_message_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_initial_message returns the message when set in data.json."""
    agent_dir = local_provider.host_dir / "agents" / str(test_agent.id)
    data_path = agent_dir / "data.json"

    # Update data.json with initial_message
    data = json.loads(data_path.read_text())
    data["initial_message"] = "Hello from test"
    data_path.write_text(json.dumps(data, indent=2))

    assert test_agent.get_initial_message() == "Hello from test"


def test_get_resume_message_returns_none_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_resume_message returns None when not set in data.json."""
    assert test_agent.get_resume_message() is None


def test_get_resume_message_returns_message_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_resume_message returns the message when set in data.json."""
    agent_dir = local_provider.host_dir / "agents" / str(test_agent.id)
    data_path = agent_dir / "data.json"

    # Update data.json with resume_message
    data = json.loads(data_path.read_text())
    data["resume_message"] = "Welcome back!"
    data_path.write_text(json.dumps(data, indent=2))

    assert test_agent.get_resume_message() == "Welcome back!"


def test_get_ready_timeout_seconds_returns_default_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_ready_timeout_seconds returns default when not set in data.json."""
    assert test_agent.get_ready_timeout_seconds() == DEFAULT_AGENT_READY_TIMEOUT_SECONDS


def test_get_ready_timeout_seconds_returns_value_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_ready_timeout_seconds returns the value when set in data.json."""
    agent_dir = local_provider.host_dir / "agents" / str(test_agent.id)
    data_path = agent_dir / "data.json"

    # Update data.json with ready_timeout_seconds
    data = json.loads(data_path.read_text())
    data["ready_timeout_seconds"] = 2.5
    data_path.write_text(json.dumps(data, indent=2))

    assert test_agent.get_ready_timeout_seconds() == 2.5


def test_get_expected_process_name_uses_command_basename(
    test_agent: BaseAgent,
) -> None:
    """Test that get_expected_process_name returns the command basename."""
    # Default command is "sleep 1000" based on create_test_agent
    assert test_agent.get_expected_process_name() == "sleep"


def test_uses_marker_based_send_message_returns_false_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that uses_marker_based_send_message returns False by default."""
    assert test_agent.uses_marker_based_send_message() is False


def test_get_tui_ready_indicator_returns_none_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_tui_ready_indicator returns None by default."""
    assert test_agent.get_tui_ready_indicator() is None


@pytest.mark.tmux
def test_send_backspace_with_noop_sends_keys_to_tmux(
    test_agent: BaseAgent,
) -> None:
    """Test that _send_backspace_with_noop sends backspaces and noop keys to tmux session."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session with some text
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'cat'",
        timeout_seconds=5.0,
    )

    try:
        # Wait for cat to start
        wait_for(
            lambda: test_agent.host.execute_command(
                f"tmux list-panes -t '{session_name}' -F '#{{pane_current_command}}'"
            ).stdout.strip()
            == "cat",
            timeout=5.0,
            error_message="cat process not ready",
        )

        # Send some text
        test_agent.host.execute_command(f"tmux send-keys -t '{session_name}' -l 'hello'")

        # Wait for text to appear
        wait_for(
            lambda: "hello" in (test_agent._capture_pane_content(session_name) or ""),
            timeout=5.0,
            error_message="text not visible in pane",
        )

        # Now send backspaces with noop - should remove some characters
        test_agent._send_backspace_with_noop(session_name, count=2)

        # Verify backspaces were processed (last 2 chars should be removed)
        content = test_agent._capture_pane_content(session_name)
        assert content is not None
        # After backspaces, "hello" should become "hel"
        assert "hel" in content
    finally:
        test_agent.host.execute_command(
            f"tmux kill-session -t '{session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


@pytest.mark.tmux
def test_send_enter_and_wait_for_signal_returns_true_when_signal_received(
    test_agent: BaseAgent,
) -> None:
    """Test that _send_enter_and_wait_for_signal returns True when tmux wait-for signal is received."""
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"
    wait_channel = f"mng-submit-{session_name}"

    # Create a tmux session
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        # Signal the channel from a background process after a short delay
        # This simulates what the UserPromptSubmit hook does
        test_agent.host.execute_command(
            f"( sleep 0.1 && tmux wait-for -S '{wait_channel}' ) &",
            timeout_seconds=1.0,
        )

        # Call the method - it should receive the signal and return True
        result = test_agent._send_enter_and_wait_for_signal(session_name, wait_channel)
        assert result is True
    finally:
        test_agent.host.execute_command(
            f"tmux kill-session -t '{session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


@pytest.mark.tmux
def test_send_enter_and_wait_for_signal_returns_false_on_timeout(
    test_agent: BaseAgent,
) -> None:
    """Test that _send_enter_and_wait_for_signal returns False when signal times out."""
    # Use a shorter timeout so the test doesn't wait the full 2 seconds
    test_agent.enter_submission_timeout_seconds = 0.2
    session_name = f"{test_agent.mng_ctx.config.prefix}{test_agent.name}"
    # Use a unique channel that won't be signaled
    wait_channel = f"mng-submit-never-signaled-{session_name}"

    # Create a tmux session
    test_agent.host.execute_command(
        f"tmux new-session -d -s '{session_name}' 'bash'",
        timeout_seconds=5.0,
    )

    try:
        # Call the method without signaling - should timeout and return False
        result = test_agent._send_enter_and_wait_for_signal(session_name, wait_channel)
        assert result is False
    finally:
        test_agent.host.execute_command(
            f"tmux kill-session -t '{session_name}' 2>/dev/null",
            timeout_seconds=5.0,
        )


# =========================================================================
# assemble_command tests
# =========================================================================


def test_assemble_command_uses_command_override(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that command_override takes highest priority."""
    config = AgentTypeConfig(command=CommandString("configured-cmd"))
    agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir, agent_config=config)

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=CommandString("override-cmd"),
    )
    assert result == CommandString("override-cmd")


def test_assemble_command_uses_config_command_when_no_override(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that agent_config.command is used when no command_override is given."""
    config = AgentTypeConfig(command=CommandString("configured-cmd"))
    agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir, agent_config=config)

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=None,
    )
    assert result == CommandString("configured-cmd")


def test_assemble_command_falls_back_to_agent_type(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that agent_type is used as command when neither override nor config command is set."""
    config = AgentTypeConfig()
    agent = create_test_agent(
        local_provider, temp_host_dir, temp_work_dir, agent_config=config, agent_type=AgentTypeName("my-custom-type")
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=None,
    )
    assert result == CommandString("my-custom-type")


def test_assemble_command_appends_cli_args(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that cli_args from config are appended to the command."""
    config = AgentTypeConfig(command=CommandString("my-cmd"), cli_args=("--flag", "value"))
    agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir, agent_config=config)

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=None,
    )
    assert result == CommandString("my-cmd --flag value")


def test_assemble_command_appends_agent_args(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that agent_args are appended to the command."""
    config = AgentTypeConfig(command=CommandString("my-cmd"))
    agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir, agent_config=config)

    result = agent.assemble_command(
        host=agent.host,
        agent_args=("--extra", "arg"),
        command_override=None,
    )
    assert result == CommandString("my-cmd --extra arg")


def test_assemble_command_appends_both_cli_and_agent_args(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_work_dir: Path,
) -> None:
    """Test that both cli_args and agent_args are appended in order."""
    config = AgentTypeConfig(command=CommandString("my-cmd"), cli_args=("--cli-flag",))
    agent = create_test_agent(local_provider, temp_host_dir, temp_work_dir, agent_config=config)

    result = agent.assemble_command(
        host=agent.host,
        agent_args=("--agent-flag",),
        command_override=None,
    )
    assert result == CommandString("my-cmd --cli-flag --agent-flag")


# =========================================================================
# _read_data tests
# =========================================================================


def test_read_data_returns_empty_dict_when_no_data_file(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that _read_data returns {} when data.json does not exist."""
    # Remove the data.json file
    data_path = local_provider.host_dir / "agents" / str(test_agent.id) / "data.json"
    data_path.unlink()

    result = test_agent._read_data()
    assert result == {}


# =========================================================================
# get_command tests
# =========================================================================


def test_get_command_returns_command_from_data(
    test_agent: BaseAgent,
) -> None:
    """Test that get_command returns the command stored in data.json."""
    # data.json was created with command="sleep 1000"
    assert test_agent.get_command() == CommandString("sleep 1000")


def test_get_command_returns_bash_when_no_command(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_command returns 'bash' when no command is in data.json."""
    # Remove the command from data.json
    data_path = local_provider.host_dir / "agents" / str(test_agent.id) / "data.json"
    data = json.loads(data_path.read_text())
    del data["command"]
    data_path.write_text(json.dumps(data, indent=2))

    assert test_agent.get_command() == CommandString("bash")


# =========================================================================
# get_permissions / set_permissions tests
# =========================================================================


def test_get_permissions_returns_empty_list_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_permissions returns an empty list when none are set."""
    assert test_agent.get_permissions() == []


def test_set_and_get_permissions(
    test_agent: BaseAgent,
) -> None:
    """Test that set_permissions persists and get_permissions retrieves them."""
    perms = [Permission("read"), Permission("write"), Permission("execute")]
    test_agent.set_permissions(perms)

    result = test_agent.get_permissions()
    assert result == perms


# =========================================================================
# get_labels / set_labels tests
# =========================================================================


def test_get_labels_returns_empty_dict_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_labels returns an empty dict when none are set."""
    assert test_agent.get_labels() == {}


def test_set_and_get_labels(
    test_agent: BaseAgent,
) -> None:
    """Test that set_labels persists and get_labels retrieves them."""
    labels = {"env": "production", "team": "backend"}
    test_agent.set_labels(labels)

    result = test_agent.get_labels()
    assert result == labels


# =========================================================================
# get_created_branch_name tests
# =========================================================================


def test_get_created_branch_name_returns_none_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_created_branch_name returns None when not set."""
    assert test_agent.get_created_branch_name() is None


def test_get_created_branch_name_returns_value_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_created_branch_name returns the branch name when set in data.json."""
    data_path = local_provider.host_dir / "agents" / str(test_agent.id) / "data.json"
    data = json.loads(data_path.read_text())
    data["created_branch_name"] = "feature/my-branch"
    data_path.write_text(json.dumps(data, indent=2))

    assert test_agent.get_created_branch_name() == "feature/my-branch"


# =========================================================================
# get_is_start_on_boot / set_is_start_on_boot tests
# =========================================================================


def test_get_is_start_on_boot_returns_false_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_is_start_on_boot returns False by default."""
    assert test_agent.get_is_start_on_boot() is False


def test_set_and_get_is_start_on_boot(
    test_agent: BaseAgent,
) -> None:
    """Test that set_is_start_on_boot persists and get_is_start_on_boot retrieves it."""
    test_agent.set_is_start_on_boot(True)
    assert test_agent.get_is_start_on_boot() is True

    test_agent.set_is_start_on_boot(False)
    assert test_agent.get_is_start_on_boot() is False


# =========================================================================
# get_reported_url tests
# =========================================================================


def test_get_reported_url_returns_none_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_reported_url returns None when no url file exists."""
    assert test_agent.get_reported_url() is None


def test_get_reported_url_returns_url_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_reported_url returns the URL from the status file."""
    status_dir = local_provider.host_dir / "agents" / str(test_agent.id) / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "url").write_text("https://example.com/agent\n")

    assert test_agent.get_reported_url() == "https://example.com/agent"


# =========================================================================
# get_reported_start_time tests
# =========================================================================


def test_get_reported_start_time_returns_none_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_reported_start_time returns None when no start_time file exists."""
    assert test_agent.get_reported_start_time() is None


def test_get_reported_start_time_returns_datetime_when_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that get_reported_start_time returns a datetime from the status file."""
    status_dir = local_provider.host_dir / "agents" / str(test_agent.id) / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    start_time = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    (status_dir / "start_time").write_text(start_time.isoformat() + "\n")

    result = test_agent.get_reported_start_time()
    assert result is not None
    assert result == start_time


# =========================================================================
# get_reported_activity_time / record_activity tests
# =========================================================================


def test_get_reported_activity_time_returns_none_when_no_activity(
    test_agent: BaseAgent,
) -> None:
    """Test that get_reported_activity_time returns None when no activity recorded."""
    assert test_agent.get_reported_activity_time(ActivitySource.USER) is None


def test_record_activity_and_get_reported_activity_time(
    test_agent: BaseAgent,
) -> None:
    """Test that record_activity writes a file and get_reported_activity_time reads its mtime."""
    before = datetime.now(timezone.utc)
    test_agent.record_activity(ActivitySource.USER)

    result = test_agent.get_reported_activity_time(ActivitySource.USER)
    assert result is not None
    # mtime should be approximately now (within a few seconds)
    delta = (result - before).total_seconds()
    assert -2.0 <= delta <= 5.0


def test_record_activity_writes_json_with_expected_fields(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that record_activity writes JSON containing time, agent_id, and agent_name."""
    test_agent.record_activity(ActivitySource.PROCESS)

    activity_path = local_provider.host_dir / "agents" / str(test_agent.id) / "activity" / "process"
    content = json.loads(activity_path.read_text())
    assert "time" in content
    assert content["agent_id"] == str(test_agent.id)
    assert content["agent_name"] == str(test_agent.name)
    assert isinstance(content["time"], int)


# =========================================================================
# get_plugin_data / set_plugin_data tests
# =========================================================================


def test_get_plugin_data_returns_empty_dict_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_plugin_data returns {} when no plugin data is set."""
    assert test_agent.get_plugin_data("my-plugin") == {}


def test_set_and_get_plugin_data(
    test_agent: BaseAgent,
) -> None:
    """Test that set_plugin_data persists and get_plugin_data retrieves it."""
    plugin_data = {"key1": "value1", "nested": {"a": 1}}
    test_agent.set_plugin_data("my-plugin", plugin_data)

    result = test_agent.get_plugin_data("my-plugin")
    assert result == plugin_data


def test_plugin_data_is_isolated_per_plugin(
    test_agent: BaseAgent,
) -> None:
    """Test that plugin data for different plugins is independent."""
    test_agent.set_plugin_data("plugin-a", {"a": 1})
    test_agent.set_plugin_data("plugin-b", {"b": 2})

    assert test_agent.get_plugin_data("plugin-a") == {"a": 1}
    assert test_agent.get_plugin_data("plugin-b") == {"b": 2}
    assert test_agent.get_plugin_data("plugin-c") == {}


# =========================================================================
# get_reported_plugin_file / set_reported_plugin_file / list_reported_plugin_files tests
# =========================================================================


def test_set_and_get_reported_plugin_file(
    test_agent: BaseAgent,
) -> None:
    """Test that set_reported_plugin_file writes and get_reported_plugin_file reads."""
    test_agent.set_reported_plugin_file("my-plugin", "config.json", '{"hello": "world"}')

    result = test_agent.get_reported_plugin_file("my-plugin", "config.json")
    assert result == '{"hello": "world"}'


def test_get_reported_plugin_file_raises_when_not_found(
    test_agent: BaseAgent,
) -> None:
    """Test that get_reported_plugin_file raises FileNotFoundError for missing files."""
    with pytest.raises(FileNotFoundError):
        test_agent.get_reported_plugin_file("nonexistent-plugin", "missing.txt")


def test_list_reported_plugin_files_returns_empty_when_none(
    test_agent: BaseAgent,
) -> None:
    """Test that list_reported_plugin_files returns [] when no files exist."""
    assert test_agent.list_reported_plugin_files("nonexistent-plugin") == []


def test_list_reported_plugin_files_returns_filenames(
    test_agent: BaseAgent,
) -> None:
    """Test that list_reported_plugin_files returns the names of files for a plugin."""
    test_agent.set_reported_plugin_file("my-plugin", "file1.txt", "content1")
    test_agent.set_reported_plugin_file("my-plugin", "file2.json", "content2")

    result = sorted(test_agent.list_reported_plugin_files("my-plugin"))
    assert result == ["file1.txt", "file2.json"]


# =========================================================================
# get_env_vars / set_env_vars / get_env_var / set_env_var tests
# =========================================================================


def test_get_env_vars_returns_empty_dict_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_env_vars returns {} when no environment file exists."""
    assert test_agent.get_env_vars() == {}


def test_set_and_get_env_vars(
    test_agent: BaseAgent,
) -> None:
    """Test that set_env_vars persists and get_env_vars retrieves them."""
    env = {"API_KEY": "secret123", "DEBUG": "true"}
    test_agent.set_env_vars(env)

    result = test_agent.get_env_vars()
    assert result == env


def test_get_env_var_returns_value_when_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_env_var returns the value for a specific key."""
    test_agent.set_env_vars({"FOO": "bar", "BAZ": "qux"})

    assert test_agent.get_env_var("FOO") == "bar"
    assert test_agent.get_env_var("BAZ") == "qux"


def test_get_env_var_returns_none_when_not_set(
    test_agent: BaseAgent,
) -> None:
    """Test that get_env_var returns None for a key that does not exist."""
    assert test_agent.get_env_var("NONEXISTENT") is None


def test_set_env_var_adds_to_existing(
    test_agent: BaseAgent,
) -> None:
    """Test that set_env_var adds a new variable without clobbering existing ones."""
    test_agent.set_env_vars({"EXISTING": "value"})
    test_agent.set_env_var("NEW_KEY", "new_value")

    assert test_agent.get_env_var("EXISTING") == "value"
    assert test_agent.get_env_var("NEW_KEY") == "new_value"


def test_set_env_var_overwrites_existing_key(
    test_agent: BaseAgent,
) -> None:
    """Test that set_env_var overwrites an existing variable."""
    test_agent.set_env_vars({"KEY": "old"})
    test_agent.set_env_var("KEY", "new")

    assert test_agent.get_env_var("KEY") == "new"


# =========================================================================
# runtime_seconds tests
# =========================================================================


def test_runtime_seconds_returns_none_when_no_start_time(
    test_agent: BaseAgent,
) -> None:
    """Test that runtime_seconds returns None when no start time is reported."""
    assert test_agent.runtime_seconds is None


def test_runtime_seconds_returns_positive_value_when_start_time_set(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """Test that runtime_seconds returns a positive value when start time is in the past."""
    status_dir = local_provider.host_dir / "agents" / str(test_agent.id) / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    # Set start time to 60 seconds ago
    start_time = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    (status_dir / "start_time").write_text(start_time.isoformat())

    result = test_agent.runtime_seconds
    assert result is not None
    # Should be at least a few years worth of seconds (the start time is in 2020)
    assert result > 100_000
