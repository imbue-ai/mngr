"""Unit tests for the connect CLI command."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.agent_selector import build_status_text
from imbue.mngr.cli.agent_selector import filter_agents
from imbue.mngr.cli.agent_selector import handle_search_key
from imbue.mngr.cli.connect import connect
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.testing import make_test_agent_details

# =============================================================================
# Tests for connect CLI option parsing
# =============================================================================


def test_connect_session_command_flag_maps_to_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The --session-command flag should parse into ConnectCliOptions.session_command.

    The command raises NotImplementedError for a non-None session_command
    before any agent resolution, so a NotImplementedError with the expected
    message proves the flag was parsed into the option (a no-op flag would
    instead proceed to agent lookup).
    """
    result = cli_runner.invoke(
        connect,
        ["my-agent", "--session-command", "bash"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert isinstance(result.exception, NotImplementedError)
    assert str(result.exception) == "--session-command is not implemented yet"


def test_connect_no_reconnect_flag_maps_to_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """The --no-reconnect flag should parse into ConnectCliOptions.reconnect=False.

    The command raises NotImplementedError when reconnect is False before any
    agent resolution, proving the flag was parsed into the option.
    """
    result = cli_runner.invoke(
        connect,
        ["my-agent", "--no-reconnect"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert isinstance(result.exception, NotImplementedError)
    assert str(result.exception) == "--no-reconnect is not implemented yet"


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
# Tests for connect CLI command
# =============================================================================
