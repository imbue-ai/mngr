"""Tests for the connect CLI command."""

import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.text import Text
from urwid.widget.wimp import SelectableIcon

from imbue.imbue_common.model_update import to_update
from imbue.mng.cli.conftest import make_test_agent_info
from imbue.mng.cli.connect import AgentSelectorState
from imbue.mng.cli.connect import ConnectCliOptions
from imbue.mng.cli.connect import SelectorInputHandler
from imbue.mng.cli.connect import _build_connection_options
from imbue.mng.cli.connect import _create_selectable_agent_item
from imbue.mng.cli.connect import _handle_selector_input
from imbue.mng.cli.connect import _refresh_agent_list
from imbue.mng.cli.connect import build_status_text
from imbue.mng.cli.connect import connect
from imbue.mng.cli.connect import filter_agents
from imbue.mng.cli.connect import handle_search_key
from imbue.mng.cli.connect import select_agent_interactively
from imbue.mng.cli.create import create
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.main import cli
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.utils.testing import cleanup_tmux_session
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists

# =============================================================================
# CLI-level integration tests for connect command
#
# These tests invoke the connect CLI command end-to-end. Because os.execvp
# replaces the test process, we intercept it via the intercepted_execvp_calls
# fixture (defined in conftest.py) to capture the args and verify the full
# CLI pipeline works.
# =============================================================================


def test_connect_no_agent_found(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that connecting to a non-existent agent raises an error."""
    result = cli_runner.invoke(
        connect,
        ["nonexistent-agent-xyz123"],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert result.exception is not None


@pytest.mark.tmux
def test_connect_cli_invokes_tmux_attach_for_named_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test the full connect CLI path: argument parsing -> agent resolution -> tmux attach."""
    agent_name = f"test-connect-cli-tmux-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name, agent_cmd="sleep 493827"
        )

        result = cli_runner.invoke(
            connect,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0, f"Connect failed with output: {result.output}"

        # Verify the CLI resolved the agent and called tmux attach with the right session
        assert len(intercepted_execvp_calls) == 1
        assert intercepted_execvp_calls[0] == ("tmux", ["tmux", "attach", "-t", session_name])


@pytest.mark.tmux
def test_connect_via_cli_group(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test calling connect through the main CLI group."""
    agent_name = f"test-connect-cli-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    try:
        # First create an agent
        create_result = cli_runner.invoke(
            cli,
            [
                "create",
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 918273",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed with: {create_result.output}"

        cli_runner.invoke(
            cli,
            ["connect", agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert len(intercepted_execvp_calls) == 1
        assert intercepted_execvp_calls[0] == ("tmux", ["tmux", "attach", "-t", session_name])

    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_connect_start_restarts_stopped_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test that --start (default) automatically restarts a stopped agent via the CLI."""
    agent_name = f"test-connect-start-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    try:
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 736291",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed with: {create_result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        # Kill the tmux session to simulate a stopped agent
        cleanup_tmux_session(session_name)
        assert not tmux_session_exists(session_name), f"Expected tmux session {session_name} to be killed"

        # Connect with --start (default), which should restart the agent
        cli_runner.invoke(
            connect,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        # Verify the tmux session was recreated before attaching
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to be restarted"
        assert len(intercepted_execvp_calls) == 1
        assert intercepted_execvp_calls[0] == ("tmux", ["tmux", "attach", "-t", session_name])

    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_connect_no_start_raises_error_for_stopped_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --no-start raises UserInputError when agent is stopped."""
    agent_name = f"test-connect-nostart-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    try:
        # Create an agent
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847362",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed with: {create_result.output}"

        # Kill the tmux session to simulate a stopped agent
        cleanup_tmux_session(session_name)
        assert not tmux_session_exists(session_name), f"Expected tmux session {session_name} to be killed"

        # Connect with --no-start, which should raise an error
        result = cli_runner.invoke(
            connect,
            [agent_name, "--no-start"],
            obj=plugin_manager,
        )

        assert result.exit_code != 0
        assert result.exception is not None
        assert "stopped" in str(result.output).lower()
        assert "automatic starting is disabled" in str(result.output)

    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.tmux
def test_connect_cli_non_interactive_selects_most_recent_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test the non-interactive code path selects the most recently created agent.

    When stdin is not a tty (simulated by providing input=""), the connect
    command should detect non-interactive mode, call list_agents, sort by
    create_time descending, and select the most recently created agent.
    """
    agent_name_old = f"test-connect-old-{int(time.time())}"
    agent_name_new = f"test-connect-new-{int(time.time())}"
    session_old = f"{mng_test_prefix}{agent_name_old}"
    session_new = f"{mng_test_prefix}{agent_name_new}"

    with tmux_session_cleanup(session_old), tmux_session_cleanup(session_new):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name_old, agent_cmd="sleep 192837"
        )
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, agent_name_new, agent_cmd="sleep 283746"
        )

        cli_runner.invoke(
            connect,
            [],
            obj=plugin_manager,
            catch_exceptions=False,
            # Providing input="" makes stdin non-tty, triggering the non-interactive path
            input="",
        )

        # Verify the CLI selected the most recently created agent
        assert len(intercepted_execvp_calls) == 1
        assert intercepted_execvp_calls[0] == ("tmux", ["tmux", "attach", "-t", session_new])


# =============================================================================
# Tests for _build_connection_options (CLI option to ConnectionOptions mapping)
# =============================================================================


def test_build_connection_options_allow_unknown_host_true(
    default_connect_cli_opts: ConnectCliOptions,
) -> None:
    """Test that allow_unknown_host=True produces is_unknown_host_allowed=True."""
    opts = default_connect_cli_opts.model_copy_update(
        to_update(default_connect_cli_opts.field_ref().allow_unknown_host, True),
    )

    connection_opts = _build_connection_options(opts)

    assert connection_opts.is_unknown_host_allowed is True


def test_build_connection_options_allow_unknown_host_default(
    default_connect_cli_opts: ConnectCliOptions,
) -> None:
    """Test that is_unknown_host_allowed defaults to False."""
    connection_opts = _build_connection_options(default_connect_cli_opts)

    assert connection_opts.is_unknown_host_allowed is False


def test_build_connection_options_maps_all_fields(
    default_connect_cli_opts: ConnectCliOptions,
) -> None:
    """Test that all ConnectCliOptions fields are correctly mapped to ConnectionOptions."""
    opts = default_connect_cli_opts.model_copy_update(
        to_update(default_connect_cli_opts.field_ref().reconnect, False),
        to_update(default_connect_cli_opts.field_ref().retry, 5),
        to_update(default_connect_cli_opts.field_ref().retry_delay, "10s"),
        to_update(default_connect_cli_opts.field_ref().attach_command, "bash"),
        to_update(default_connect_cli_opts.field_ref().allow_unknown_host, True),
    )

    connection_opts = _build_connection_options(opts)

    assert connection_opts.is_reconnect is False
    assert connection_opts.retry_count == 5
    assert connection_opts.retry_delay == "10s"
    assert connection_opts.attach_command == "bash"
    assert connection_opts.is_unknown_host_allowed is True


# =============================================================================
# Unit tests for filter_agents
# =============================================================================


def test_filter_agents_no_filters() -> None:
    """Test filter_agents with no filters applied."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.STOPPED),
        make_test_agent_info("gamma", AgentLifecycleState.RUNNING),
    ]

    result = filter_agents(agents, hide_stopped=False, search_query="")
    assert len(result) == 3


def test_filter_agents_hide_stopped() -> None:
    """Test filter_agents with hide_stopped=True."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.STOPPED),
        make_test_agent_info("gamma", AgentLifecycleState.RUNNING),
    ]

    result = filter_agents(agents, hide_stopped=True, search_query="")
    assert len(result) == 2
    assert all(a.state != AgentLifecycleState.STOPPED for a in result)


def test_filter_agents_search_query() -> None:
    """Test filter_agents with search query."""
    agents = [
        make_test_agent_info("test-alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("test-beta", AgentLifecycleState.RUNNING),
        make_test_agent_info("other", AgentLifecycleState.RUNNING),
    ]

    # Case-insensitive search
    result = filter_agents(agents, hide_stopped=False, search_query="ALPHA")
    assert len(result) == 1
    assert result[0].name == AgentName("test-alpha")

    # Partial match
    result = filter_agents(agents, hide_stopped=False, search_query="test")
    assert len(result) == 2


def test_filter_agents_combined_filters() -> None:
    """Test filter_agents with both filters applied."""
    agents = [
        make_test_agent_info("test-alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("test-beta", AgentLifecycleState.STOPPED),
        make_test_agent_info("other", AgentLifecycleState.RUNNING),
    ]

    result = filter_agents(agents, hide_stopped=True, search_query="test")
    assert len(result) == 1
    assert result[0].name == AgentName("test-alpha")


# =============================================================================
# Unit tests for build_status_text
# =============================================================================


def test_build_status_text_default() -> None:
    """Test build_status_text with default state."""
    status = build_status_text(search_query="", hide_stopped=False)
    assert "Status: Ready" in status
    assert "Type to search" in status
    assert "Filter: All agents" in status


def test_build_status_text_with_search() -> None:
    """Test build_status_text with search query."""
    status = build_status_text(search_query="test", hide_stopped=False)
    assert "Search: test" in status
    assert "Type to search" not in status


def test_build_status_text_hide_stopped() -> None:
    """Test build_status_text with hide_stopped=True."""
    status = build_status_text(search_query="", hide_stopped=True)
    assert "Filter: Hiding stopped" in status


# =============================================================================
# Unit tests for handle_search_key
# =============================================================================


def test_handle_search_key_backspace() -> None:
    """Test handle_search_key removes last character on backspace."""
    new_query, should_refresh = handle_search_key(
        key="backspace",
        is_printable=False,
        character=None,
        current_query="test",
    )
    assert new_query == "tes"
    assert should_refresh is True


def test_handle_search_key_backspace_empty() -> None:
    """Test handle_search_key does nothing on backspace with empty query."""
    new_query, should_refresh = handle_search_key(
        key="backspace",
        is_printable=False,
        character=None,
        current_query="",
    )
    assert new_query == ""
    assert should_refresh is False


def test_handle_search_key_printable() -> None:
    """Test handle_search_key adds printable characters."""
    new_query, should_refresh = handle_search_key(
        key="a",
        is_printable=True,
        character="a",
        current_query="test",
    )
    assert new_query == "testa"
    assert should_refresh is True


def test_handle_search_key_other() -> None:
    """Test handle_search_key passes through other keys."""
    new_query, should_refresh = handle_search_key(
        key="up",
        is_printable=False,
        character=None,
        current_query="test",
    )
    assert new_query == "test"
    assert should_refresh is False


# =============================================================================
# Urwid Agent Selector UI Tests
# =============================================================================


def test_create_selectable_agent_item_displays_agent_info() -> None:
    """Test that _create_selectable_agent_item creates a selectable widget."""
    agent = make_test_agent_info("test-agent", AgentLifecycleState.RUNNING)

    widget = _create_selectable_agent_item(agent, name_width=20, state_width=10)

    assert isinstance(widget, AttrMap)
    inner_widget = widget.original_widget
    # Widget is now a SelectableIcon so ListBox can navigate between items
    assert isinstance(inner_widget, SelectableIcon)
    assert widget.selectable() is True


def test_create_selectable_agent_item_stopped_state() -> None:
    """Test that _create_selectable_agent_item shows stopped state correctly."""
    agent = make_test_agent_info("stopped-agent", AgentLifecycleState.STOPPED)

    widget = _create_selectable_agent_item(agent, name_width=20, state_width=10)

    inner_widget = widget.original_widget
    assert isinstance(inner_widget, SelectableIcon)
    assert widget.selectable() is True


def _create_test_selector_state(agents: list[AgentInfo]) -> AgentSelectorState:
    """Create an AgentSelectorState for testing without refreshing the list."""
    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])
    status_text = Text("")
    # Calculate reasonable default widths for tests
    name_width = max((len(str(a.name)) for a in agents), default=10)
    state_width = max((len(a.state.value) for a in agents), default=7)
    return AgentSelectorState(
        agents=agents,
        list_walker=list_walker,
        status_text=status_text,
        name_width=name_width,
        state_width=state_width,
    )


def _create_and_refresh_test_state(agents: list[AgentInfo]) -> AgentSelectorState:
    """Create an AgentSelectorState and refresh it for testing."""
    state = _create_test_selector_state(agents)
    _refresh_agent_list(state)
    return state


def test_agent_selector_state_initializes_with_defaults() -> None:
    """Test that AgentSelectorState initializes with correct defaults."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.STOPPED),
    ]

    state = _create_test_selector_state(agents)

    assert state.agents == agents
    assert state.filtered_agents == []
    assert state.result is None
    assert state.hide_stopped is False
    assert state.search_query == ""


def test_refresh_agent_list_populates_list_walker() -> None:
    """Test that _refresh_agent_list populates the list walker with agents."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.RUNNING),
        make_test_agent_info("gamma", AgentLifecycleState.STOPPED),
    ]
    state = _create_and_refresh_test_state(agents)

    assert len(state.list_walker) == 3
    assert len(state.filtered_agents) == 3


def test_refresh_agent_list_applies_hide_stopped_filter() -> None:
    """Test that _refresh_agent_list respects hide_stopped setting."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.STOPPED),
    ]
    state = _create_test_selector_state(agents)
    state.hide_stopped = True

    _refresh_agent_list(state)

    assert len(state.list_walker) == 1
    assert len(state.filtered_agents) == 1
    assert state.filtered_agents[0].name == AgentName("alpha")


def test_refresh_agent_list_applies_search_filter() -> None:
    """Test that _refresh_agent_list respects search_query setting."""
    agents = [
        make_test_agent_info("alpha-test", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta-prod", AgentLifecycleState.RUNNING),
    ]
    state = _create_test_selector_state(agents)
    state.search_query = "alpha"

    _refresh_agent_list(state)

    assert len(state.list_walker) == 1
    assert len(state.filtered_agents) == 1
    assert state.filtered_agents[0].name == AgentName("alpha-test")


def test_refresh_agent_list_updates_status_text() -> None:
    """Test that _refresh_agent_list updates the status text widget."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_test_selector_state(agents)
    state.search_query = "search-term"

    _refresh_agent_list(state)

    status_content = state.status_text.get_text()[0]
    assert "search-term" in status_content


def test_refresh_agent_list_sets_focus_on_first_item() -> None:
    """Test that _refresh_agent_list sets focus on the first item when items exist."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.RUNNING),
    ]
    state = _create_and_refresh_test_state(agents)

    _, focus_index = state.list_walker.get_focus()
    assert focus_index == 0


def test_handle_selector_input_ctrl_r_toggles_hide_stopped() -> None:
    """Test that Ctrl+R toggles the hide_stopped filter."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.STOPPED),
    ]
    state = _create_and_refresh_test_state(agents)

    assert state.hide_stopped is False
    assert len(state.filtered_agents) == 2

    _handle_selector_input(state, "ctrl r")

    assert state.hide_stopped is True
    assert len(state.filtered_agents) == 1


def test_handle_selector_input_ctrl_c_clears_search_query() -> None:
    """Test that Ctrl+C clears the search query when not empty."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)
    state.search_query = "test"

    _handle_selector_input(state, "ctrl c")

    assert state.search_query == ""


def test_handle_selector_input_ctrl_c_double_tap_exits() -> None:
    """Test that double Ctrl+C (within 500ms) raises ExitMainLoop."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)

    # First Ctrl-c with empty query records the time
    _handle_selector_input(state, "ctrl c")

    # Second Ctrl-c within 500ms should exit
    with pytest.raises(ExitMainLoop):
        _handle_selector_input(state, "ctrl c")


def test_handle_selector_input_enter_selects_focused_agent() -> None:
    """Test that Enter selects the currently focused agent."""
    agents = [
        make_test_agent_info("alpha", AgentLifecycleState.RUNNING),
        make_test_agent_info("beta", AgentLifecycleState.RUNNING),
    ]
    state = _create_and_refresh_test_state(agents)

    with pytest.raises(ExitMainLoop):
        _handle_selector_input(state, "enter")

    assert state.result is not None
    assert state.result.name == AgentName("alpha")


def test_handle_selector_input_enter_with_empty_list_sets_no_result() -> None:
    """Test that Enter with empty list doesn't set a result."""
    agents: list[AgentInfo] = []
    state = _create_and_refresh_test_state(agents)

    with pytest.raises(ExitMainLoop):
        _handle_selector_input(state, "enter")

    assert state.result is None


def test_handle_selector_input_arrow_keys_pass_through() -> None:
    """Test that arrow keys pass through (return False) for ListBox to handle."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)

    # Arrow keys should return False to let ListBox handle them
    assert _handle_selector_input(state, "up") is False
    assert _handle_selector_input(state, "down") is False
    assert _handle_selector_input(state, "page up") is False
    assert _handle_selector_input(state, "page down") is False


def test_handle_selector_input_printable_key_updates_search() -> None:
    """Test that printable keys update the search query."""
    agents = [
        make_test_agent_info("xyz-agent", AgentLifecycleState.RUNNING),
        make_test_agent_info("foo-agent", AgentLifecycleState.RUNNING),
    ]
    state = _create_and_refresh_test_state(agents)

    _handle_selector_input(state, "x")

    assert state.search_query == "x"
    assert len(state.filtered_agents) == 1
    assert state.filtered_agents[0].name == AgentName("xyz-agent")


def test_handle_selector_input_backspace_removes_last_character() -> None:
    """Test that backspace removes the last character from search query."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)
    state.search_query = "alph"

    _handle_selector_input(state, "backspace")

    assert state.search_query == "alp"


def test_selector_input_handler_calls_handle_selector_input() -> None:
    """Test that SelectorInputHandler delegates to _handle_selector_input."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)
    handler = SelectorInputHandler(state=state)

    handler("a")

    assert state.search_query == "a"


def test_selector_input_handler_ignores_mouse_events() -> None:
    """Test that SelectorInputHandler returns None for mouse events (tuples)."""
    agents = [make_test_agent_info("alpha", AgentLifecycleState.RUNNING)]
    state = _create_and_refresh_test_state(agents)
    handler = SelectorInputHandler(state=state)

    result = handler(("mouse press", 1, 0, 0))

    assert result is None
    assert state.search_query == ""


def test_select_agent_interactively_returns_none_for_empty_list() -> None:
    """Test that select_agent_interactively returns None when given empty list."""
    agents: list[AgentInfo] = []

    result = select_agent_interactively(agents)

    assert result is None
