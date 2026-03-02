"""Unit tests for the connect CLI command."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mng.cli.connect import ConnectCliOptions
from imbue.mng.cli.connect import _build_connection_options
from imbue.mng.cli.connect import build_status_text
from imbue.mng.cli.connect import connect
from imbue.mng.cli.connect import filter_agents
from imbue.mng.cli.connect import handle_search_key
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import ProviderInstanceName

# =============================================================================
# Helper functions for creating test data
# =============================================================================


def _make_agent_info(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
) -> AgentInfo:
    """Create an AgentInfo for testing."""
    return AgentInfo(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        start_on_boot=True,
        state=state,
        host=HostInfo(
            id=HostId.generate(),
            name="test-host",
            provider_name=ProviderInstanceName("local"),
        ),
    )


# =============================================================================
# Tests for ConnectCliOptions
# =============================================================================


def test_connect_cli_options_can_be_instantiated() -> None:
    """Test that ConnectCliOptions can be instantiated with all required fields."""
    opts = ConnectCliOptions(
        agent="my-agent",
        start=True,
        reconnect=True,
        message=None,
        message_file=None,
        ready_timeout=60.0,
        retry=3,
        retry_delay="5s",
        attach_command=None,
        allow_unknown_host=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
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
        _make_agent_info("agent-1", AgentLifecycleState.RUNNING),
        _make_agent_info("agent-2", AgentLifecycleState.STOPPED),
    ]
    result = filter_agents(agents, hide_stopped=False, search_query="")
    assert len(result) == 2


def test_filter_agents_hides_stopped() -> None:
    """filter_agents should hide stopped agents when hide_stopped is True."""
    agents = [
        _make_agent_info("agent-1", AgentLifecycleState.RUNNING),
        _make_agent_info("agent-2", AgentLifecycleState.STOPPED),
        _make_agent_info("agent-3", AgentLifecycleState.RUNNING),
    ]
    result = filter_agents(agents, hide_stopped=True, search_query="")
    assert len(result) == 2
    assert all(a.state != AgentLifecycleState.STOPPED for a in result)


def test_filter_agents_filters_by_search_query() -> None:
    """filter_agents should filter by search query (case insensitive)."""
    agents = [
        _make_agent_info("my-task-1"),
        _make_agent_info("other-agent"),
        _make_agent_info("MY-TASK-2"),
    ]
    result = filter_agents(agents, hide_stopped=False, search_query="task")
    assert len(result) == 2
    assert result[0].name == AgentName("my-task-1")
    assert result[1].name == AgentName("MY-TASK-2")


def test_filter_agents_combines_filters() -> None:
    """filter_agents should combine hide_stopped and search_query filters."""
    agents = [
        _make_agent_info("task-running", AgentLifecycleState.RUNNING),
        _make_agent_info("task-stopped", AgentLifecycleState.STOPPED),
        _make_agent_info("other-running", AgentLifecycleState.RUNNING),
    ]
    result = filter_agents(agents, hide_stopped=True, search_query="task")
    assert len(result) == 1
    assert result[0].name == AgentName("task-running")


def test_filter_agents_returns_empty_on_no_match() -> None:
    """filter_agents should return empty list when no agents match."""
    agents = [_make_agent_info("agent-1")]
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
    opts = ConnectCliOptions(
        agent="my-agent",
        start=True,
        reconnect=True,
        message=None,
        message_file=None,
        ready_timeout=60.0,
        retry=3,
        retry_delay="5s",
        attach_command=None,
        allow_unknown_host=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    conn_opts = _build_connection_options(opts)
    assert conn_opts.is_reconnect is True
    assert conn_opts.retry_count == 3
    assert conn_opts.retry_delay == "5s"
    assert conn_opts.attach_command is None
    assert conn_opts.is_unknown_host_allowed is False


def test_build_connection_options_custom_values() -> None:
    """_build_connection_options should map custom CLI values correctly."""
    opts = ConnectCliOptions(
        agent="my-agent",
        start=True,
        reconnect=False,
        message=None,
        message_file=None,
        ready_timeout=60.0,
        retry=5,
        retry_delay="10s",
        attach_command="ssh user@host",
        allow_unknown_host=True,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
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


def test_connect_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that connect --help works and exits 0."""
    result = cli_runner.invoke(
        connect,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "connect" in result.output.lower()
