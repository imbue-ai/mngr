"""Tests for BaseAgent lifecycle state detection and data methods."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.base_agent import quote_agent_args
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import ActivitySource
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import InvalidName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session


@pytest.fixture
def test_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> BaseAgent:
    # SendKeysAgent (a BaseAgent subclass) so the send-keys methods are present for the
    # message-sending tests; all base-behavior tests are unaffected (it adds only send_message).
    return create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=SendKeysAgent,
    )


@pytest.mark.tmux
def test_lifecycle_state_stopped_when_no_tmux_session(
    test_agent: BaseAgent,
) -> None:
    """Test that agent is STOPPED when there is no tmux session."""
    state = test_agent.get_lifecycle_state()
    assert state == AgentLifecycleState.STOPPED


def _create_running_agent(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    # unique sleep duration to avoid collisions with other tests
    sleep_duration: int,
) -> tuple[BaseAgent, str]:
    """Create an agent with a running tmux session and active file.

    Returns the agent and its tmux session name. Caller must clean up
    the session (e.g. with cleanup_tmux_session).
    """
    test_agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )
    session_name = f"{test_agent.mngr_ctx.config.prefix}{test_agent.name}"
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name

    # Create a tmux session and run the expected command. The primary window is
    # named to match production (and thus the agent's tmux_target), which targets
    # the window by name rather than the literal :0 index.
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'sleep {sleep_duration}'",
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
    temp_work_dir: Path,
) -> None:
    """Test that agent is RUNNING when tmux session exists with expected process and active file."""
    test_agent, session_name = _create_running_agent(local_provider, temp_work_dir, 847291)

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
    temp_work_dir: Path,
) -> None:
    """Test that is_running returns True when tmux session exists with expected process and active file."""
    test_agent, session_name = _create_running_agent(local_provider, temp_work_dir, 847293)

    try:
        wait_for(
            lambda: test_agent.is_running(),
            error_message="Expected is_running() to return True for running agent",
        )
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_lifecycle_state_running_unknown_agent_type_when_different_process_exists(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that agent is RUNNING_UNKNOWN_AGENT_TYPE when tmux session exists with
    a different process and the agent type is not registered."""
    # Use a name that is deliberately NOT in the test-placeholder agent-type
    # registration, and pass is_type_registered=False so the create_test_agent
    # helper does not register it on the fly. That way check_agent_type_known
    # returns False and the lifecycle logic reports RUNNING_UNKNOWN_AGENT_TYPE
    # rather than REPLACED.
    unregistered_agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=AgentTypeName("lifecycle-unregistered-type"),
        extra_data=None,
        agent_class=BaseAgent,
        is_type_registered=False,
    )
    session_name = f"{unregistered_agent.mngr_ctx.config.prefix}{unregistered_agent.name}"
    window_name = unregistered_agent.mngr_ctx.config.tmux.primary_window_name

    # Create a tmux session with a different command (cat waits for input indefinitely).
    # The window is named to match the agent's tmux_target (which targets by name).
    unregistered_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'cat'",
        timeout_seconds=5.0,
    )

    try:
        # There's a race condition where tmux spawns a shell first, then execs the command.
        # During that brief window, pane_current_command shows the shell, giving DONE.
        wait_for(
            lambda: unregistered_agent.get_lifecycle_state() == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE,
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
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name

    # Create a tmux session with the primary window named to match the agent's
    # tmux_target (which targets by name). The session's shell has no child
    # process, so the agent reports DONE.
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}'",
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
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name

    # Create a tmux session and run the expected command. The primary window is
    # named to match the agent's tmux_target (which targets by name).
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'sleep 1000'",
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
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name

    # Create a tmux session and run the expected command. The primary window is
    # named to match the agent's tmux_target (which targets by name).
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}' 'sleep 1000'",
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
    """get_ready_timeout_seconds falls back to MngrConfig.agent_ready_timeout when data.json omits it."""
    assert test_agent.get_ready_timeout_seconds() == MngrConfig.model_fields["agent_ready_timeout"].default


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


def test_tmux_target_uses_exact_match_named_primary_window(
    test_agent: BaseAgent,
) -> None:
    """tmux_target should return a TmuxWindowTarget pinned to the named primary window.

    The window pin protects against additional windows (watchers, ttyd) routing
    target resolution to the wrong pane. Targeting by name (``tmux.primary_window_name``,
    default ``agent``) instead of ``:0`` keeps this correct regardless of the user's
    tmux ``base-index``. When rendered via as_shell_arg(), the leading ``=`` forces
    exact session-name matching; without it, tmux silently falls back to prefix
    matching and a query for ``mngr-foo`` would match a live session called
    ``mngr-foo-bar`` once ``mngr-foo`` is gone.
    """
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name
    assert window_name == "agent"
    target = test_agent.tmux_target
    assert isinstance(target, TmuxWindowTarget)
    assert target.session_name == test_agent.session_name
    assert target.window == window_name
    assert target.as_shell_arg() == f"={test_agent.session_name}:agent"


@pytest.mark.tmux
def test_capture_pane_content_targets_requested_window(
    test_agent: BaseAgent,
) -> None:
    """capture_pane_content(window=...) should read the requested window, not window 0.

    The agent runs in window 0, but sessions can hold extra windows (watchers,
    ttyd, manually-opened terminals). Passing an explicit window must capture that
    window's pane rather than the agent's primary one.
    """
    session_name = test_agent.session_name
    window_name = test_agent.mngr_ctx.config.tmux.primary_window_name
    window_zero_marker = "WINDOW_ZERO_MARKER"
    window_one_marker = "WINDOW_ONE_MARKER"

    # Name the primary window so the default capture (which targets it by name) finds it.
    test_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -n '{window_name}' -x 200 -y 24 'echo {window_zero_marker}; sleep 493827'",
        timeout_seconds=5.0,
    )
    try:
        session_target = TmuxSessionTarget(session_name=session_name)
        test_agent.host.execute_idempotent_command(
            f"tmux new-window -t {session_target.as_shell_arg()} -d 'echo {window_one_marker}; sleep 493827'",
            timeout_seconds=5.0,
        )

        def _default_capture_shows_window_zero() -> bool:
            content = test_agent.capture_pane_content() or ""
            return window_zero_marker in content

        wait_for(
            _default_capture_shows_window_zero,
            error_message=f"Expected default capture to contain {window_zero_marker!r}",
        )

        def _window_one_capture_shows_window_one() -> bool:
            content = test_agent.capture_pane_content(window=1) or ""
            return window_one_marker in content

        wait_for(
            _window_one_capture_shows_window_one,
            error_message=f"Expected window-1 capture to contain {window_one_marker!r}",
        )

        # Cross-check: window 1's content is distinct from the default (window 0) content.
        assert window_zero_marker not in (test_agent.capture_pane_content(window=1) or "")
        assert window_one_marker not in (test_agent.capture_pane_content() or "")
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_send_tmux_literal_keys_short_message_with_leading_dash(
    test_agent: BaseAgent,
) -> None:
    """A message starting with `-` must round-trip through `tmux send-keys -l` to the pane.

    This is a regression test: without the `--` end-of-options separator, tmux's
    argv parser treats the leading dash as a flag and errors with
    `invalid flag --`, so the message never reaches the pane.
    """
    # The fixture builds a SendKeysAgent; narrow so the send-keys method is in view.
    assert isinstance(test_agent, SendKeysAgent)
    send_keys_agent: SendKeysAgent = test_agent
    session_name = f"{send_keys_agent.mngr_ctx.config.prefix}{send_keys_agent.name}"
    tmux_target = TmuxWindowTarget(session_name=session_name, window=0)
    message = "--model gemma --flag-leading-message"

    # `cat` echoes typed characters via the PTY's line discipline, so the
    # message becomes visible in the pane without needing to press Enter.
    send_keys_agent.host.execute_idempotent_command(
        f"tmux new-session -d -s '{session_name}' -x 200 -y 24 'cat'",
        timeout_seconds=5.0,
    )

    try:
        # If the bug is back, this raises SendMessageError with "invalid flag --".
        send_keys_agent._send_tmux_literal_keys(tmux_target, message)

        def _message_visible() -> bool:
            result = test_agent.host.execute_idempotent_command(
                f"tmux capture-pane -t {tmux_target.as_shell_arg()} -p",
                timeout_seconds=5.0,
            )
            return message in result.stdout

        wait_for(_message_visible, error_message=f"Expected pane to contain {message!r}")
    finally:
        cleanup_tmux_session(session_name)


# =========================================================================
# assemble_command tests
# =========================================================================


def test_assemble_command_uses_command_override(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that command_override takes highest priority."""
    config = AgentTypeConfig(command=CommandString("configured-cmd"))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=CommandString("override-cmd"),
    )
    assert result == CommandString("override-cmd")


def test_assemble_command_uses_config_command_when_no_override(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that agent_config.command is used when no command_override is given."""
    config = AgentTypeConfig(command=CommandString("configured-cmd"))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=None,
    )
    assert result == CommandString("configured-cmd")


def test_assemble_command_raises_when_no_base_and_no_args(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that assemble_command raises when neither override, config command, nor agent_args provide a base."""
    config = AgentTypeConfig()
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=AgentTypeName("generic"),
        extra_data=None,
        agent_class=BaseAgent,
    )

    with pytest.raises(UserInputError, match=r"has no command to run"):
        agent.assemble_command(
            host=agent.host,
            agent_args=(),
            command_override=None,
        )


def test_assemble_command_appends_cli_args(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that cli_args from config are appended to the command."""
    config = AgentTypeConfig(command=CommandString("my-cmd"), cli_args=("--flag", "value"))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=(),
        command_override=None,
    )
    assert result == CommandString("my-cmd --flag value")


def test_assemble_command_appends_agent_args(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that agent_args are appended to the command."""
    config = AgentTypeConfig(command=CommandString("my-cmd"))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=("--extra", "arg"),
        command_override=None,
    )
    assert result == CommandString("my-cmd --extra arg")


def test_quote_agent_args_quotes_special_chars_and_leaves_plain_args() -> None:
    """quote_agent_args wraps values needing escaping and leaves already-safe tokens untouched."""
    assert quote_agent_args(()) == ()
    assert quote_agent_args(("--flag", "value")) == ("--flag", "value")
    assert quote_agent_args(("--model", "Gemini 3.5 Flash (Medium)")) == (
        "--model",
        "'Gemini 3.5 Flash (Medium)'",
    )


def test_assemble_command_shell_quotes_agent_args_with_special_chars(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """agent_args with spaces/parens are shell-quoted so the command stays valid.

    Regression test: passing ``--model "Gemini 3.5 Flash (Medium)"`` used to splice
    the raw value into the shell-evaluated command, so bash word-split it and parsed
    ``(Medium)`` as a subshell ("syntax error near unexpected token `('").
    """
    config = AgentTypeConfig(command=CommandString("agy"))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    result = agent.assemble_command(
        host=agent.host,
        agent_args=("--model", "Gemini 3.5 Flash (Medium)"),
        command_override=None,
    )
    assert result == CommandString("agy --model 'Gemini 3.5 Flash (Medium)'")
    # The model value must be a single shell token (no bare parens/spaces).
    assert "'Gemini 3.5 Flash (Medium)'" in str(result)


def test_assemble_command_appends_both_cli_and_agent_args(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """Test that both cli_args and agent_args are appended in order."""
    config = AgentTypeConfig(command=CommandString("my-cmd"), cli_args=("--cli-flag",))
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=config,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

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
        self.host_dir = Path("/tmp/stub-host")

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

    def read_text_file(self, path: Path, **kwargs: object) -> str:
        raise FileNotFoundError(path)


def _create_named_agent_with_stub_host(
    temp_mngr_ctx: MngrContext,
    stub: _StubHost,
    name: AgentName,
    cls: type[SendKeysAgent] = SendKeysAgent,
    **kwargs: Any,
) -> SendKeysAgent:
    """Create an agent with a stub host for command recording.

    Uses model_construct to bypass Pydantic validation so the stub host
    (which does not implement the full OnlineHostInterface) can be used.
    Accepts a cls parameter to create subclass instances and **kwargs
    for additional fields defined on those subclasses.
    """
    return cls.model_construct(
        id=AgentId.generate(),
        name=name,
        agent_type=AgentTypeName("generic"),
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
    cls: type[SendKeysAgent] = SendKeysAgent,
    **kwargs: Any,
) -> SendKeysAgent:
    # Default to SendKeysAgent so the send-keys methods (_send_tmux_literal_keys /
    # _send_message_simple) are available; it is a BaseAgent for every other test.
    return _create_named_agent_with_stub_host(temp_mngr_ctx, stub, AgentName("stub-agent"), cls, **kwargs)


def test_send_tmux_literal_keys_short_message_uses_send_keys(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Short messages should use tmux send-keys -l."""
    stub = _StubHost()
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    agent._send_tmux_literal_keys(TmuxWindowTarget(session_name="mngr-test", window=0), "hello")

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
    agent._send_tmux_literal_keys(TmuxWindowTarget(session_name="mngr-test", window=0), long_message)

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
        agent._send_tmux_literal_keys(TmuxWindowTarget(session_name="mngr-test", window=0), "x" * 1024)


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
        agent._send_tmux_literal_keys(TmuxWindowTarget(session_name="mngr-test", window=0), "x" * 1024)


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
        agent._send_tmux_literal_keys(TmuxWindowTarget(session_name="mngr-test", window=0), "hello")


# =========================================================================
# Unnamed-primary-window migration tests (pre-upgrade in-flight agents)
# =========================================================================


def test_migrate_unnamed_primary_window_renames_lowest_index_window(
    temp_mngr_ctx: MngrContext,
) -> None:
    """The migration guards on has-session and renames the session's lowest-index window by name."""
    stub = _StubHost(command_results=[CommandResult(success=True, stdout="", stderr="")])
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    is_migrated = agent._migrate_unnamed_primary_window()

    assert is_migrated is True
    assert len(stub.executed_commands) == 1
    command = stub.executed_commands[0]
    session_target = TmuxSessionTarget(session_name=agent.session_name).as_shell_arg()
    # Guarded by has-session so a missing session is a no-op.
    assert f"tmux has-session -t {session_target}" in command
    # Selects the lowest window index (robust to base-index and extra windows) ...
    assert f"tmux list-windows -t {session_target} -F '#I'" in command
    assert "sort -n | head -n 1" in command
    # ... and renames that window to the configured primary window name.
    assert "tmux rename-window -t" in command
    assert temp_mngr_ctx.config.tmux.primary_window_name in command


def test_get_lifecycle_state_migrates_on_name_miss_then_reprobes(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A by-name probe miss on an existing session triggers a rename and a successful re-probe."""
    stub = _StubHost(
        command_results=[
            # Initial by-name probe misses (unnamed pre-upgrade primary window).
            CommandResult(success=True, stdout="", stderr=""),
            # The one-shot migration rename succeeds.
            CommandResult(success=True, stdout="", stderr=""),
            # The re-probe now resolves the renamed primary window.
            CommandResult(success=True, stdout="0|node|12345", stderr=""),
            # ps output for descendant detection.
            CommandResult(success=True, stdout="12345 1 node\n", stderr=""),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    agent.get_lifecycle_state()

    # First probe, then rename, then re-probe (a correctly-named session would skip the latter two).
    assert "list-panes" in stub.executed_commands[0]
    assert "rename-window" in stub.executed_commands[1]
    assert "list-panes" in stub.executed_commands[2]


def test_get_lifecycle_state_skips_migration_when_name_probe_hits(
    temp_mngr_ctx: MngrContext,
) -> None:
    """A correctly-named session resolves on the first probe and never issues a rename."""
    stub = _StubHost(
        command_results=[
            CommandResult(success=True, stdout="0|node|12345", stderr=""),
            CommandResult(success=True, stdout="12345 1 node\n", stderr=""),
        ]
    )
    agent = _create_agent_with_stub_host(temp_mngr_ctx, stub)

    agent.get_lifecycle_state()

    assert not any("rename-window" in command for command in stub.executed_commands)


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

    agent._send_message_simple(TmuxWindowTarget(session_name="mngr-test", window=0), "hello")

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
        agent._send_message_simple(TmuxWindowTarget(session_name="mngr-test", window=0), "hello")


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
