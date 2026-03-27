"""Unit tests for the connect CLI command."""

from imbue.mngr.cli.connect import ConnectCliOptions
from imbue.mngr.cli.connect import _build_connection_options
from imbue.mngr.cli.connect import build_status_text
from imbue.mngr.cli.connect import filter_agents
from imbue.mngr.cli.connect import handle_search_key
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.testing import make_test_agent_details

# =============================================================================
# Helpers
# =============================================================================


def _make_connect_opts(
    agent: str | None = "my-agent",
    start: bool = True,
    reconnect: bool = True,
    retry: int = 3,
    retry_delay: str = "5s",
    attach_command: str | None = None,
    allow_unknown_host: bool = False,
    output_format: str = "human",
    quiet: bool = False,
    verbose: int = 0,
    log_file: str | None = None,
    log_commands: bool | None = None,
    log_command_output: bool | None = None,
    log_env_vars: bool | None = None,
    project_context_path: str | None = None,
    plugin: tuple[str, ...] = (),
    disable_plugin: tuple[str, ...] = (),
) -> ConnectCliOptions:
    """Create a ConnectCliOptions with sensible defaults, allowing overrides."""
    return ConnectCliOptions(
        agent=agent,
        start=start,
        reconnect=reconnect,
        retry=retry,
        retry_delay=retry_delay,
        attach_command=attach_command,
        allow_unknown_host=allow_unknown_host,
        output_format=output_format,
        quiet=quiet,
        verbose=verbose,
        log_file=log_file,
        log_commands=log_commands,
        log_command_output=log_command_output,
        log_env_vars=log_env_vars,
        project_context_path=project_context_path,
        plugin=plugin,
        disable_plugin=disable_plugin,
    )


# =============================================================================
# Tests for ConnectCliOptions
# =============================================================================


def test_connect_cli_options_can_be_instantiated() -> None:
    """Test that ConnectCliOptions can be instantiated with all required fields."""
    opts = _make_connect_opts()
    assert opts.agent == "my-agent"
    assert opts.start is True
    assert opts.reconnect is True
    assert opts.retry == 3


# =============================================================================
# Tests for filter_agents
# =============================================================================


def test_filter_agents_returns_all_when_no_filters() -> None:
    """filter_agents should return all agents when no filters applied."""
    agents = [
        make_test_agent_details("agent-1", AgentLifecycleState.RUNNING),
        make_test_agent_details("agent-2", AgentLifecycleState.STOPPED),
    ]
    result = filter_agents(agents, hide_stopped=False, search_query="")
    assert len(result) == 2


def test_filter_agents_hides_stopped() -> None:
    """filter_agents should hide stopped agents when hide_stopped is True."""
    agents = [
        make_test_agent_details("agent-1", AgentLifecycleState.RUNNING),
        make_test_agent_details("agent-2", AgentLifecycleState.STOPPED),
        make_test_agent_details("agent-3", AgentLifecycleState.RUNNING),
    ]
    result = filter_agents(agents, hide_stopped=True, search_query="")
    assert len(result) == 2
    assert all(a.state != AgentLifecycleState.STOPPED for a in result)


def test_filter_agents_filters_by_search_query() -> None:
    """filter_agents should filter by search query (case insensitive)."""
    agents = [
        make_test_agent_details("my-task-1"),
        make_test_agent_details("other-agent"),
        make_test_agent_details("MY-TASK-2"),
    ]
    result = filter_agents(agents, hide_stopped=False, search_query="task")
    assert len(result) == 2
    assert result[0].name == AgentName("my-task-1")
    assert result[1].name == AgentName("MY-TASK-2")


def test_filter_agents_combines_filters() -> None:
    """filter_agents should combine hide_stopped and search_query filters."""
    agents = [
        make_test_agent_details("task-running", AgentLifecycleState.RUNNING),
        make_test_agent_details("task-stopped", AgentLifecycleState.STOPPED),
        make_test_agent_details("other-running", AgentLifecycleState.RUNNING),
    ]
    result = filter_agents(agents, hide_stopped=True, search_query="task")
    assert len(result) == 1
    assert result[0].name == AgentName("task-running")


def test_filter_agents_returns_empty_on_no_match() -> None:
    """filter_agents should return empty list when no agents match."""
    agents = [make_test_agent_details("agent-1")]
    result = filter_agents(agents, hide_stopped=False, search_query="nonexistent")
    assert result == []


# =============================================================================
# Tests for build_status_text
# =============================================================================


def test_build_status_text_default() -> None:
    """build_status_text should show default state when no search and no filter."""
    text = build_status_text(search_query="", hide_stopped=False)
    assert "Status: Ready" in text
    assert "Type to search" in text
    assert "Filter: All agents" in text


def test_build_status_text_with_search() -> None:
    """build_status_text should show search query when provided."""
    text = build_status_text(search_query="task", hide_stopped=False)
    assert "Search: task" in text
    assert "Type to search" not in text


def test_build_status_text_with_hide_stopped() -> None:
    """build_status_text should show hiding stopped filter."""
    text = build_status_text(search_query="", hide_stopped=True)
    assert "Filter: Hiding stopped" in text
    assert "Filter: All agents" not in text


def test_build_status_text_with_both_filters() -> None:
    """build_status_text should show both search and stopped filter."""
    text = build_status_text(search_query="my-agent", hide_stopped=True)
    assert "Search: my-agent" in text
    assert "Filter: Hiding stopped" in text


# =============================================================================
# Tests for handle_search_key
# =============================================================================


def test_handle_search_key_backspace_removes_last_char() -> None:
    """handle_search_key should remove last character on backspace."""
    new_query, should_refresh = handle_search_key("backspace", False, None, "abc")
    assert new_query == "ab"
    assert should_refresh is True


def test_handle_search_key_backspace_on_empty_query() -> None:
    """handle_search_key should not refresh on backspace with empty query."""
    new_query, should_refresh = handle_search_key("backspace", False, None, "")
    assert new_query == ""
    assert should_refresh is False


def test_handle_search_key_printable_character() -> None:
    """handle_search_key should append printable characters to the query."""
    new_query, should_refresh = handle_search_key("a", True, "a", "test")
    assert new_query == "testa"
    assert should_refresh is True


def test_handle_search_key_non_printable_ignored() -> None:
    """handle_search_key should ignore non-printable keys."""
    new_query, should_refresh = handle_search_key("ctrl a", False, None, "test")
    assert new_query == "test"
    assert should_refresh is False


def test_handle_search_key_printable_but_no_character() -> None:
    """handle_search_key should not modify query if character is None."""
    new_query, should_refresh = handle_search_key("tab", True, None, "test")
    assert new_query == "test"
    assert should_refresh is False


# =============================================================================
# Tests for _build_connection_options
# =============================================================================


def test_build_connection_options_default_values() -> None:
    """_build_connection_options should create ConnectionOptions from CLI options."""
    opts = _make_connect_opts()
    conn_opts = _build_connection_options(opts)
    assert conn_opts.is_reconnect is True
    assert conn_opts.retry_count == 3
    assert conn_opts.retry_delay == "5s"
    assert conn_opts.attach_command is None
    assert conn_opts.is_unknown_host_allowed is False


def test_build_connection_options_custom_values() -> None:
    """_build_connection_options should map custom CLI values correctly."""
    opts = _make_connect_opts(
        reconnect=False,
        retry=5,
        retry_delay="10s",
        attach_command="ssh user@host",
        allow_unknown_host=True,
    )
    conn_opts = _build_connection_options(opts)
    assert conn_opts.is_reconnect is False
    assert conn_opts.retry_count == 5
    assert conn_opts.retry_delay == "10s"
    assert conn_opts.attach_command == "ssh user@host"
    assert conn_opts.is_unknown_host_allowed is True


# =============================================================================
# Tests for connect CLI command
# =============================================================================
