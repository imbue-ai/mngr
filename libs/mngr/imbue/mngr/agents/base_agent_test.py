"""Tests for BaseAgent lifecycle state detection and data methods."""

import json
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import _check_paste_content
from imbue.mngr.agents.base_agent import _normalize_for_match
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import DEFAULT_AGENT_READY_TIMEOUT_SECONDS
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import InvalidName
from imbue.mngr.primitives import Permission
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import get_short_random_string


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
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
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
        mngr_ctx=local_provider.mngr_ctx,
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
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent 'sleep {sleep_duration}'",
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
def test_lifecycle_state_running_unknown_agent_type_when_different_process_exists(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is RUNNING_UNKNOWN_AGENT_TYPE when tmux session exists with
    a different process and the agent type is not registered."""
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session with a different command (cat waits for input indefinitely)
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent 'cat'",
        timeout_seconds=5.0,
    )

    try:
        # The test agent has type "test" which is not registered, so with an
        # unrecognized process running it should be RUNNING_UNKNOWN_AGENT_TYPE
        # (not REPLACED, which is for known agent types).
        # There's a race condition where tmux spawns a shell first, then execs the command.
        # During that brief window, pane_current_command shows the shell, giving DONE.
        wait_for(
            lambda: test_agent.get_lifecycle_state() == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
            error_message="Expected agent lifecycle state to be RUNNING_UNKNOWN_AGENT_TYPE",
        )
    finally:
        # Clean up tmux session and all its processes
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_done_when_no_process_in_pane(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is DONE when tmux session exists but no process is running."""
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session, then manually stop the process inside it
    # First create it with a long-running command
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent",
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
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent 'sleep 1000'",
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
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"

    # Create a tmux session and run the expected command
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent 'sleep 1000'",
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


def test_uses_paste_detection_send_returns_false_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that uses_paste_detection_send returns False by default."""
    assert test_agent.uses_paste_detection_send() is False


def test_tmux_target_appends_agent_window_name(
    test_agent: BaseAgent,
) -> None:
    """tmux_target should return session_name:agent to target the named agent window."""
    assert test_agent.tmux_target == f"{test_agent.session_name}:agent"


def test_get_tui_ready_indicator_returns_none_by_default(
    test_agent: BaseAgent,
) -> None:
    """Test that get_tui_ready_indicator returns None by default."""
    assert test_agent.get_tui_ready_indicator() is None


def test_normalize_for_match_strips_non_alnum_and_lowercases() -> None:
    """_normalize_for_match should strip non-alphanumeric chars and lowercase."""
    assert _normalize_for_match("Hello, World!") == "helloworld"
    assert _normalize_for_match("foo-bar_baz 123") == "foobarbaz123"
    assert _normalize_for_match("") == ""
    assert _normalize_for_match("  \n\t  ") == ""


def test_check_paste_content_detects_paste_indicator() -> None:
    """_check_paste_content returns True when tmux paste indicator is present."""
    assert _check_paste_content("some text\n[Pasted text 123 chars]\nmore text", "anything") is True


def test_check_paste_content_detects_fuzzy_content_match() -> None:
    """_check_paste_content returns True when normalized message tail is found in pane."""
    pane = "prompt> hello world this is a test message"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is True


def test_check_paste_content_returns_false_when_no_match() -> None:
    """_check_paste_content returns False when neither paste indicator nor content match."""
    pane = "prompt> totally different content"
    assert _check_paste_content(pane, "Hello, World! This is a test message") is False


def test_check_paste_content_handles_empty_message() -> None:
    """_check_paste_content returns True for empty messages (nothing to verify)."""
    assert _check_paste_content("some content", "") is True


@pytest.mark.tmux
@pytest.mark.skipif(shutil.which("timeout") is None, reason="requires GNU timeout (coreutils)")
def test_send_enter_and_wait_for_signal_returns_true_when_signal_received(
    test_agent: BaseAgent,
) -> None:
    """Test that _send_enter_and_wait_for_signal returns True when tmux wait-for signal is received."""
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"
    tmux_target = f"{session_name}:agent"
    wait_channel = f"mngr-submit-{session_name}"

    # Run a shell that signals the channel on receiving Enter (simulates the
    # UserPromptSubmit hook). Using `read` blocks until Enter is received,
    # then `tmux wait-for -S` fires the signal — deterministic, no timing race.
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent "
        f"\"bash -c 'read line && tmux wait-for -S {wait_channel}'\"",
        timeout_seconds=5.0,
    )

    try:
        result = test_agent._send_enter_and_wait_for_signal(tmux_target, wait_channel)
        assert result is True
    finally:
        test_agent.host.execute_idempotent_command(
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
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"
    tmux_target = f"{session_name}:agent"
    # Use a unique channel that won't be signaled
    wait_channel = f"mngr-submit-never-signaled-{session_name}"

    # Create a tmux session with named agent window (matching tmux_target)
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n agent 'bash'",
        timeout_seconds=5.0,
    )

    try:
        # Call the method without signaling - should timeout and return False
        result = test_agent._send_enter_and_wait_for_signal(tmux_target, wait_channel)
        assert result is False
    finally:
        test_agent.host.execute_idempotent_command(
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


# =========================================================================
# _send_tmux_literal_keys tests
# =========================================================================


class _StubHost:
    """Minimal stub for testing _send_tmux_literal_keys without real tmux.

    Records execute_command and write_text_file calls for assertion.
    """

    def __init__(
        self,
        command_results: list[CommandResult] | None = None,
    ) -> None:
        default_result = CommandResult(success=True, stdout="", stderr="")
        self._command_results = list(command_results) if command_results else []
        self._default_result = default_result
        self.executed_commands: list[str] = []
        self.written_files: list[tuple[Path, str]] = []

    def _execute_command(self, command: str, **kwargs: object) -> CommandResult:
        self.executed_commands.append(command)
        if self._command_results:
            return self._command_results.pop(0)
        return self._default_result

    def execute_idempotent_command(self, command: str, **kwargs: object) -> CommandResult:
        return self._execute_command(command, **kwargs)

    def execute_stateful_command(self, command: str, **kwargs: object) -> CommandResult:
        return self._execute_command(command, **kwargs)

    def write_text_file(self, path: Path, content: str, **kwargs: object) -> None:
        self.written_files.append((path, content))


def _create_named_agent_with_stub_host(
    temp_mngr_ctx: MngrContext,
    stub: _StubHost,
    name: AgentName,
    cls: type[BaseAgent] = BaseAgent,
    **kwargs: Any,
) -> BaseAgent:
    """Create an agent with a stub host for command recording.

    Uses model_construct to bypass Pydantic validation so the stub host
    (which does not implement the full OnlineHostInterface) can be used.
    Accepts a cls parameter to create subclass instances and **kwargs
    for additional fields (e.g. enter_submission_timeout_seconds).
    """
    return cls.model_construct(
        id=AgentId.generate(),
        name=name,
        agent_type=AgentTypeName("test"),
        work_dir=Path("/tmp/stub-work"),
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        host=stub,
        mngr_ctx=temp_mngr_ctx,
        agent_config=AgentTypeConfig(command=CommandString("sleep 1000")),
        **kwargs,
    )


def _create_agent_with_stub_host(
    temp_mngr_ctx: MngrContext,
    stub: _StubHost,
    cls: type[BaseAgent] = BaseAgent,
    **kwargs: Any,
) -> BaseAgent:
    return _create_named_agent_with_stub_host(temp_mngr_ctx, stub, AgentName("stub-agent"), cls, **kwargs)


def test_send_tmux_literal_keys_short_message_uses_send_keys(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Short messages should use tmux send-keys -l."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    agent._send_tmux_literal_keys("mngr-test:0", "hello")

    assert len(stub.executed_commands) == 1
    assert "send-keys" in stub.executed_commands[0]
    assert "-l" in stub.executed_commands[0]
    assert len(stub.written_files) == 0


def test_send_tmux_literal_keys_long_message_uses_load_buffer(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Messages >= 1024 chars should use write_text_file + load-buffer + paste-buffer."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    long_message = "x" * 1024
    agent._send_tmux_literal_keys("mngr-test:0", long_message)

    # Should write the file
    assert len(stub.written_files) == 1
    assert stub.written_files[0][1] == long_message

    # Then execute load-buffer, paste-buffer, and cleanup
    assert len(stub.executed_commands) == 3
    assert "load-buffer" in stub.executed_commands[0]
    assert "-b" in stub.executed_commands[0]
    assert "paste-buffer" in stub.executed_commands[1]
    assert "-b" in stub.executed_commands[1]
    assert "delete-buffer" in stub.executed_commands[2]
    assert "rm -f" in stub.executed_commands[2]


def test_send_tmux_literal_keys_long_message_raises_on_load_buffer_failure(
    temp_mngr_ctx: MngrContext,
) -> None:
    """load-buffer failure should raise SendMessageError."""
    stub = _StubHost(
        command_results=[
            CommandResult(success=False, stdout="", stderr="load failed"),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    with pytest.raises(SendMessageError, match="load-buffer failed"):
        agent._send_tmux_literal_keys("mngr-test:0", "x" * 1024)


def test_send_tmux_literal_keys_long_message_raises_on_paste_buffer_failure(
    temp_mngr_ctx: MngrContext,
) -> None:
    """paste-buffer failure should raise SendMessageError."""
    stub = _StubHost(
        command_results=[
            CommandResult(success=True, stdout="", stderr=""),
            CommandResult(success=False, stdout="", stderr="paste failed"),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    with pytest.raises(SendMessageError, match="paste-buffer failed"):
        agent._send_tmux_literal_keys("mngr-test:0", "x" * 1024)


def test_send_tmux_literal_keys_short_message_raises_on_send_keys_failure(
    temp_mngr_ctx: MngrContext,
) -> None:
    """send-keys failure should raise SendMessageError."""
    stub = _StubHost(
        command_results=[
            CommandResult(success=False, stdout="", stderr="command too long"),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    with pytest.raises(SendMessageError, match="send-keys failed"):
        agent._send_tmux_literal_keys("mngr-test:0", "hello")


def test_agent_name_rejects_slash() -> None:
    """AgentName must reject names containing '/' to prevent path issues."""
    with pytest.raises(InvalidName):
        AgentName("foo/bar")


# =========================================================================
# _send_message_simple tests
# =========================================================================


def test_send_message_simple_sends_keys_and_enter(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_send_message_simple should send keys then send Enter."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    agent._send_message_simple("mngr-test:0", "hello")

    assert len(stub.executed_commands) == 2
    assert "send-keys" in stub.executed_commands[0]
    assert "-l" in stub.executed_commands[0]
    assert "Enter" in stub.executed_commands[1]


def test_send_message_simple_raises_on_enter_failure(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_send_message_simple should raise when Enter fails."""
    stub = _StubHost(
        command_results=[
            CommandResult(success=True, stdout="", stderr=""),
            CommandResult(success=False, stdout="", stderr="enter failed"),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    with pytest.raises(SendMessageError, match="send-keys Enter failed"):
        agent._send_message_simple("mngr-test:0", "hello")


# =========================================================================
# _raise_send_timeout tests
# =========================================================================


def test_raise_send_timeout_raises_send_message_error(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_raise_send_timeout should raise SendMessageError with the given reason."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    with pytest.raises(SendMessageError, match="timeout reason"):
        agent._raise_send_timeout("mngr-test:0", "timeout reason")


# =========================================================================
# _get_command_basename tests
# =========================================================================


def test_get_command_basename_full_path(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_get_command_basename should extract basename from a full path."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    assert agent._get_command_basename(CommandString("/usr/bin/python3 script.py")) == "python3"


def test_get_command_basename_simple_command(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_get_command_basename should handle a simple command name."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    assert agent._get_command_basename(CommandString("sleep 1000")) == "sleep"


def test_get_command_basename_single_word(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_get_command_basename should return the command itself for a single word."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    assert agent._get_command_basename(CommandString("python3")) == "python3"


def test_get_command_basename_strips_leading_subshell_syntax(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_get_command_basename should strip leading '(' from subshell-wrapped commands."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    assert agent._get_command_basename(CommandString("( /usr/bin/script.sh session ) &")) == "script.sh"


# =========================================================================
# get_reported_activity_record tests
# =========================================================================


def test_get_reported_activity_record_returns_none_when_no_activity(
    test_agent: BaseAgent,
) -> None:
    """get_reported_activity_record should return None when no activity recorded."""
    assert test_agent.get_reported_activity_record(ActivitySource.USER) is None


def test_get_reported_activity_record_returns_json_after_recording(
    test_agent: BaseAgent,
) -> None:
    """get_reported_activity_record should return JSON content after recording."""
    test_agent.record_activity(ActivitySource.PROCESS)

    result = test_agent.get_reported_activity_record(ActivitySource.PROCESS)
    assert result is not None
    data = json.loads(result)
    assert data["agent_id"] == str(test_agent.id)
    assert data["agent_name"] == str(test_agent.name)


# =========================================================================
# _write_data tests
# =========================================================================


def test_write_data_persists_to_file(
    test_agent: BaseAgent,
    local_provider: LocalProviderInstance,
) -> None:
    """_write_data should persist data to data.json."""
    data = test_agent._read_data()
    data["custom_field"] = "custom_value"
    test_agent._write_data(data)

    # Read back and verify
    result = test_agent._read_data()
    assert result["custom_field"] == "custom_value"


# =========================================================================
# _check_paste_content edge cases
# =========================================================================


def test_check_paste_content_short_message_tail() -> None:
    """_check_paste_content with a short message should use its full length as probe."""
    pane = "prompt> abc"
    # Message shorter than 60 chars
    assert _check_paste_content(pane, "abc") is True


def test_check_paste_content_long_message_uses_tail() -> None:
    """_check_paste_content with a long message should match on the last 60 chars."""
    # Create a message longer than 60 chars where only the tail matches the pane
    tail = "a" * 60
    message = "x" * 100 + tail
    pane = "prompt> " + tail
    assert _check_paste_content(pane, message) is True
