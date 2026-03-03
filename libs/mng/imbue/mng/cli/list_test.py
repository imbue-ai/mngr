"""Tests for CLI list command helpers."""

import json
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from io import StringIO
from typing import Any

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.list import ListResult
from imbue.mng.cli.conftest import make_test_agent_info
from imbue.mng.cli.list import _StreamingHumanRenderer
from imbue.mng.cli.list import _StreamingTemplateEmitter
from imbue.mng.cli.list import _compute_column_widths
from imbue.mng.cli.list import _emit_template_output
from imbue.mng.cli.list import _format_streaming_agent_row
from imbue.mng.cli.list import _format_streaming_header_row
from imbue.mng.cli.list import _format_value_as_string
from imbue.mng.cli.list import _get_field_value
from imbue.mng.cli.list import _get_header_label
from imbue.mng.cli.list import _get_sortable_value
from imbue.mng.cli.list import _is_streaming_eligible
from imbue.mng.cli.list import _parse_slice_spec
from imbue.mng.cli.list import _render_format_template
from imbue.mng.cli.list import _should_use_streaming_mode
from imbue.mng.cli.list import _sort_agents
from imbue.mng.cli.list import list_command
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName


def _create_test_snapshot(name: str, idx: int) -> SnapshotInfo:
    """Create a test SnapshotInfo for testing."""
    return SnapshotInfo(
        id=SnapshotId(f"snap-test-{idx}"),
        name=SnapshotName(name),
        created_at=datetime.now(timezone.utc),
        recency_idx=idx,
    )


# =============================================================================
# Tests for _parse_slice_spec
# =============================================================================


def test_parse_slice_spec_single_index_zero() -> None:
    """_parse_slice_spec should parse single index 0."""
    result = _parse_slice_spec("0")
    assert result == 0


def test_parse_slice_spec_single_index_positive() -> None:
    """_parse_slice_spec should parse positive index."""
    result = _parse_slice_spec("5")
    assert result == 5


def test_parse_slice_spec_single_index_negative() -> None:
    """_parse_slice_spec should parse negative index."""
    result = _parse_slice_spec("-1")
    assert result == -1


def test_parse_slice_spec_slice_start_only() -> None:
    """_parse_slice_spec should parse slice with start only."""
    result = _parse_slice_spec("2:")
    assert result == slice(2, None)


def test_parse_slice_spec_slice_stop_only() -> None:
    """_parse_slice_spec should parse slice with stop only."""
    result = _parse_slice_spec(":3")
    assert result == slice(None, 3)


def test_parse_slice_spec_slice_start_and_stop() -> None:
    """_parse_slice_spec should parse slice with start and stop."""
    result = _parse_slice_spec("1:4")
    assert result == slice(1, 4)


def test_parse_slice_spec_slice_with_step() -> None:
    """_parse_slice_spec should parse slice with step."""
    result = _parse_slice_spec("0:10:2")
    assert result == slice(0, 10, 2)


def test_parse_slice_spec_full_slice() -> None:
    """_parse_slice_spec should parse full slice (::)."""
    result = _parse_slice_spec("::")
    assert result == slice(None, None, None)


def test_parse_slice_spec_with_whitespace() -> None:
    """_parse_slice_spec should handle whitespace."""
    result = _parse_slice_spec(" 3 ")
    assert result == 3


def test_parse_slice_spec_invalid_too_many_colons() -> None:
    """_parse_slice_spec should return None for invalid spec with too many colons."""
    result = _parse_slice_spec("1:2:3:4")
    assert result is None


def test_parse_slice_spec_invalid_non_integer() -> None:
    """_parse_slice_spec should return None for non-integer spec."""
    result = _parse_slice_spec("abc")
    assert result is None


def test_parse_slice_spec_invalid_non_integer_in_slice() -> None:
    """_parse_slice_spec should return None for non-integer in slice."""
    result = _parse_slice_spec("1:abc")
    assert result is None


# =============================================================================
# Tests for _format_value_as_string
# =============================================================================


def test_format_value_as_string_none_returns_empty() -> None:
    """_format_value_as_string should return empty string for None."""
    assert _format_value_as_string(None) == ""


def test_format_value_as_string_enum_returns_uppercase_value() -> None:
    """_format_value_as_string should return uppercase enum value."""
    result = _format_value_as_string(AgentLifecycleState.RUNNING)
    assert result == AgentLifecycleState.RUNNING.value


def test_format_value_as_string_string_returns_unchanged() -> None:
    """_format_value_as_string should return string unchanged."""
    assert _format_value_as_string("hello") == "hello"


def test_format_value_as_string_int_returns_string() -> None:
    """_format_value_as_string should convert int to string."""
    assert _format_value_as_string(42) == "42"


def test_format_value_as_string_snapshot_uses_name() -> None:
    """_format_value_as_string should use name for SnapshotInfo."""
    snapshot = _create_test_snapshot("my-snapshot", 0)
    result = _format_value_as_string(snapshot)
    assert result == "my-snapshot"


# =============================================================================
# Tests for _get_field_value with bracket notation
# =============================================================================


def test_get_field_value_simple_field() -> None:
    """_get_field_value should extract simple field."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "name")
    assert result == "test-agent"


def test_get_field_value_nested_field() -> None:
    """_get_field_value should extract nested field."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "host.name")
    assert result == "test-host"


def test_get_field_value_provider_name() -> None:
    """_get_field_value should extract host.provider_name."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "host.provider_name")
    assert result == "local"


def test_get_field_value_list_index_first() -> None:
    """_get_field_value should extract first element with [0]."""
    snapshots = [
        _create_test_snapshot("snap-first", 0),
        _create_test_snapshot("snap-second", 1),
        _create_test_snapshot("snap-third", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[0]")
    assert result == "snap-first"


def test_get_field_value_list_index_last() -> None:
    """_get_field_value should extract last element with [-1]."""
    snapshots = [
        _create_test_snapshot("snap-first", 0),
        _create_test_snapshot("snap-second", 1),
        _create_test_snapshot("snap-third", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[-1]")
    assert result == "snap-third"


def test_get_field_value_list_index_middle() -> None:
    """_get_field_value should extract middle element with index."""
    snapshots = [
        _create_test_snapshot("snap-first", 0),
        _create_test_snapshot("snap-second", 1),
        _create_test_snapshot("snap-third", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[1]")
    assert result == "snap-second"


def test_get_field_value_list_slice_first_n() -> None:
    """_get_field_value should extract first N elements with [:N]."""
    snapshots = [
        _create_test_snapshot("snap-first", 0),
        _create_test_snapshot("snap-second", 1),
        _create_test_snapshot("snap-third", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[:2]")
    assert result == "snap-first, snap-second"


def test_get_field_value_list_slice_last_n() -> None:
    """_get_field_value should extract last N elements with [-N:]."""
    snapshots = [
        _create_test_snapshot("snap-first", 0),
        _create_test_snapshot("snap-second", 1),
        _create_test_snapshot("snap-third", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[-2:]")
    assert result == "snap-second, snap-third"


def test_get_field_value_list_slice_range() -> None:
    """_get_field_value should extract range with [start:stop]."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
        _create_test_snapshot("snap-2", 2),
        _create_test_snapshot("snap-3", 3),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[1:3]")
    assert result == "snap-1, snap-2"


def test_get_field_value_list_index_out_of_bounds() -> None:
    """_get_field_value should return empty string for out of bounds index."""
    snapshots = [_create_test_snapshot("snap-only", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[5]")
    assert result == ""


def test_get_field_value_list_empty() -> None:
    """_get_field_value should return empty string for empty list with index."""
    agent = make_test_agent_info(snapshots=[])
    result = _get_field_value(agent, "host.snapshots[0]")
    assert result == ""


def test_get_field_value_list_empty_slice() -> None:
    """_get_field_value should return empty string for empty list with slice."""
    agent = make_test_agent_info(snapshots=[])
    result = _get_field_value(agent, "host.snapshots[:3]")
    assert result == ""


def test_get_field_value_bracket_on_non_list() -> None:
    """_get_field_value should return empty string for bracket on non-list field."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "host.name[0]")
    # host.name is a string, but we explicitly exclude strings from bracket indexing
    # for clearer behavior (strings would return single characters which is confusing)
    assert result == ""


def test_get_field_value_invalid_field() -> None:
    """_get_field_value should return empty string for invalid field."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "nonexistent")
    assert result == ""


def test_get_field_value_invalid_nested_field() -> None:
    """_get_field_value should return empty string for invalid nested field."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "host.nonexistent")
    assert result == ""


def test_get_field_value_invalid_slice_syntax() -> None:
    """_get_field_value should return empty string for invalid slice syntax."""
    snapshots = [_create_test_snapshot("snap-only", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[abc]")
    assert result == ""


def test_get_field_value_too_many_colons_in_slice() -> None:
    """_get_field_value should return empty string for too many colons in slice."""
    snapshots = [_create_test_snapshot("snap-only", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[1:2:3:4]")
    assert result == ""


# =============================================================================
# Edge case tests for slicing
# =============================================================================


def test_get_field_value_step_zero_returns_empty() -> None:
    """_get_field_value should return empty string for step=0 (invalid slice)."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    # [::0] is invalid in Python - slice step cannot be zero
    result = _get_field_value(agent, "host.snapshots[::0]")
    assert result == ""


def test_get_field_value_empty_brackets_returns_empty() -> None:
    """_get_field_value should return empty string for empty brackets []."""
    snapshots = [_create_test_snapshot("snap-0", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[]")
    assert result == ""


def test_get_field_value_multiple_brackets_returns_empty() -> None:
    """_get_field_value should return empty string for multiple brackets [0][1]."""
    snapshots = [_create_test_snapshot("snap-0", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[0][0]")
    assert result == ""


def test_get_field_value_reverse_slice() -> None:
    """_get_field_value should support reverse slice [::-1]."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
        _create_test_snapshot("snap-2", 2),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[::-1]")
    assert result == "snap-2, snap-1, snap-0"


def test_get_field_value_negative_slice_bounds() -> None:
    """_get_field_value should support negative slice bounds [-3:-1]."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
        _create_test_snapshot("snap-2", 2),
        _create_test_snapshot("snap-3", 3),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[-3:-1]")
    assert result == "snap-1, snap-2"


def test_get_field_value_slice_with_step() -> None:
    """_get_field_value should support slice with step [::2]."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
        _create_test_snapshot("snap-2", 2),
        _create_test_snapshot("snap-3", 3),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[::2]")
    assert result == "snap-0, snap-2"


def test_get_field_value_whitespace_in_brackets() -> None:
    """_get_field_value should handle whitespace inside brackets."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[ 0 ]")
    assert result == "snap-0"


def test_get_field_value_float_index_returns_empty() -> None:
    """_get_field_value should return empty string for float index [1.5]."""
    snapshots = [_create_test_snapshot("snap-0", 0)]
    agent = make_test_agent_info(snapshots=snapshots)
    result = _get_field_value(agent, "host.snapshots[1.5]")
    assert result == ""


def test_get_field_value_slice_beyond_list_length() -> None:
    """_get_field_value should return available elements for slice beyond list."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    # Slice [0:100] on a 2-element list should return both elements
    result = _get_field_value(agent, "host.snapshots[0:100]")
    assert result == "snap-0, snap-1"


def test_get_field_value_slice_no_match_returns_empty() -> None:
    """_get_field_value should return empty string for slice with no matching elements."""
    snapshots = [
        _create_test_snapshot("snap-0", 0),
        _create_test_snapshot("snap-1", 1),
    ]
    agent = make_test_agent_info(snapshots=snapshots)
    # Slice [10:20] on a 2-element list should return empty
    result = _get_field_value(agent, "host.snapshots[10:20]")
    assert result == ""


def test_parse_slice_spec_negative_step() -> None:
    """_parse_slice_spec should parse negative step."""
    result = _parse_slice_spec("::-1")
    assert result == slice(None, None, -1)


def test_parse_slice_spec_negative_start_and_stop() -> None:
    """_parse_slice_spec should parse negative start and stop."""
    result = _parse_slice_spec("-3:-1")
    assert result == slice(-3, -1)


# =============================================================================
# Tests for _get_field_value with host plugin dict access
# =============================================================================


def test_get_field_value_host_plugin_top_level() -> None:
    """_get_field_value should access host plugin data via dict key traversal."""
    agent = make_test_agent_info(host_plugin={"aws": {"iam_user": "admin"}})
    result = _get_field_value(agent, "host.plugin.aws.iam_user")
    assert result == "admin"


def test_get_field_value_host_plugin_nested() -> None:
    """_get_field_value should access nested host plugin data."""
    agent = make_test_agent_info(host_plugin={"monitoring": {"endpoint": "https://example.com", "enabled": True}})
    result = _get_field_value(agent, "host.plugin.monitoring.endpoint")
    assert result == "https://example.com"


def test_get_field_value_host_plugin_missing_plugin_name() -> None:
    """_get_field_value should return empty for nonexistent plugin name."""
    agent = make_test_agent_info(host_plugin={})
    result = _get_field_value(agent, "host.plugin.nonexistent.field")
    assert result == ""


def test_get_field_value_host_plugin_missing_field() -> None:
    """_get_field_value should return empty for nonexistent field within plugin."""
    agent = make_test_agent_info(host_plugin={"aws": {"iam_user": "admin"}})
    result = _get_field_value(agent, "host.plugin.aws.nonexistent")
    assert result == ""


def test_get_field_value_host_plugin_whole_dict() -> None:
    """_get_field_value should format a dict value when accessing a plugin namespace."""
    agent = make_test_agent_info(host_plugin={"aws": {"iam_user": "admin"}})
    result = _get_field_value(agent, "host.plugin.aws")
    assert result == "iam_user=admin"


# =============================================================================
# Tests for _get_sortable_value with host plugin dict access
# =============================================================================


def test_get_sortable_value_host_plugin_field() -> None:
    """_get_sortable_value should return raw value for host plugin field."""
    agent = make_test_agent_info(host_plugin={"aws": {"iam_user": "admin"}})
    result = _get_sortable_value(agent, "host.plugin.aws.iam_user")
    assert result == "admin"


def test_get_sortable_value_host_plugin_missing() -> None:
    """_get_sortable_value should return None for nonexistent host plugin field."""
    agent = make_test_agent_info(host_plugin={})
    result = _get_sortable_value(agent, "host.plugin.nonexistent.field")
    assert result is None


# =============================================================================
# Tests for _get_sortable_value
# =============================================================================


def test_get_sortable_value_simple_field() -> None:
    """_get_sortable_value should return raw value for simple field."""
    agent = make_test_agent_info()
    result = _get_sortable_value(agent, "name")
    assert result == AgentName("test-agent")


def test_get_sortable_value_nested_field() -> None:
    """_get_sortable_value should return raw value for nested field."""
    agent = make_test_agent_info()
    result = _get_sortable_value(agent, "host.name")
    assert result == "test-host"


def test_get_sortable_value_provider_name() -> None:
    """_get_sortable_value should extract host.provider_name."""
    agent = make_test_agent_info()
    result = _get_sortable_value(agent, "host.provider_name")
    assert result == "local"


def test_get_sortable_value_invalid_field() -> None:
    """_get_sortable_value should return None for invalid field."""
    agent = make_test_agent_info()
    result = _get_sortable_value(agent, "nonexistent")
    assert result is None


# =============================================================================
# Tests for _sort_agents
# =============================================================================


def test_sort_agents_by_name_ascending() -> None:
    """_sort_agents should sort by name in ascending order."""
    agents = [
        make_test_agent_info(name="charlie"),
        make_test_agent_info(name="alpha"),
        make_test_agent_info(name="bravo"),
    ]
    result = _sort_agents(agents, "name", reverse=False)
    assert [str(a.name) for a in result] == ["alpha", "bravo", "charlie"]


def test_sort_agents_by_name_descending() -> None:
    """_sort_agents should sort by name in descending order."""
    agents = [
        make_test_agent_info(name="alpha"),
        make_test_agent_info(name="charlie"),
        make_test_agent_info(name="bravo"),
    ]
    result = _sort_agents(agents, "name", reverse=True)
    assert [str(a.name) for a in result] == ["charlie", "bravo", "alpha"]


# =============================================================================
# Tests for _format_streaming_header_row and _format_streaming_agent_row
# =============================================================================


def test_format_streaming_header_row_uses_custom_labels() -> None:
    """_format_streaming_header_row should produce custom header labels."""
    fields = ["name", "host.name", "state"]
    widths = _compute_column_widths(fields, 120)
    result = _format_streaming_header_row(fields, widths)
    assert "NAME" in result
    assert "HOST" in result
    assert "STATE" in result


def test_format_streaming_agent_row_extracts_field_values() -> None:
    """_format_streaming_agent_row should extract and format agent field values."""
    agent = make_test_agent_info()
    fields = ["name", "host.provider_name"]
    widths = _compute_column_widths(fields, 120)
    result = _format_streaming_agent_row(agent, fields, widths)
    assert "test-agent" in result
    assert "local" in result


def test_compute_column_widths_respects_minimums() -> None:
    """_compute_column_widths should never go below minimum widths."""
    fields = ["name", "state"]
    widths = _compute_column_widths(fields, 120)
    assert widths["name"] >= 20
    assert widths["state"] >= 10


def test_compute_column_widths_expands_expandable_columns() -> None:
    """_compute_column_widths should give extra space to expandable columns."""
    fields = ["name", "state"]
    widths = _compute_column_widths(fields, 120)
    # name is expandable, state is not -- name should get all the extra space
    assert widths["name"] > 20
    assert widths["state"] == 10


# =============================================================================
# Tests for _StreamingHumanRenderer
# =============================================================================


def _create_streaming_renderer(
    fields: list[str],
    is_tty: bool,
    output: StringIO,
) -> _StreamingHumanRenderer:
    """Create and initialize a streaming renderer for tests."""
    return _StreamingHumanRenderer(fields=fields, is_tty=is_tty, output=output)


def test_streaming_renderer_non_tty_no_ansi_codes() -> None:
    """Non-TTY streaming output should contain no ANSI escape codes."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name", "state"], is_tty=False, output=captured)
    renderer.start()
    renderer(make_test_agent_info())
    renderer.finish()

    output = captured.getvalue()
    assert "\x1b" not in output
    assert "test-agent" in output
    assert "NAME" in output


def test_streaming_renderer_tty_includes_status_line() -> None:
    """TTY streaming output should include status line with ANSI codes."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()

    output = captured.getvalue()
    assert "Searching..." in output


def test_streaming_renderer_tty_shows_count_after_agent() -> None:
    """TTY streaming should update status line with count after agent is received."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer(make_test_agent_info())

    output = captured.getvalue()
    assert "(1 found)" in output


def test_streaming_renderer_finish_no_agents_shows_no_agents_found(capsys) -> None:
    """Streaming renderer should indicate no agents when finishing with zero results."""
    captured = StringIO()

    renderer = _create_streaming_renderer(fields=["name"], is_tty=False, output=captured)
    renderer.start()
    renderer.finish()

    # write_human_line writes to sys.stdout, so check captured stdout
    stdout_output = capsys.readouterr().out
    assert "No agents found" in stdout_output


def test_streaming_renderer_thread_safety() -> None:
    """Streaming renderer should handle concurrent calls without data corruption."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=False, output=captured)
    renderer.start()

    # Send agents from multiple threads concurrently
    agent_count = 20
    threads: list[threading.Thread] = []
    for idx in range(agent_count):
        agent = make_test_agent_info(name=f"agent-{idx}")
        thread = threading.Thread(target=renderer, args=(agent,))
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    renderer.finish()

    output = captured.getvalue()
    # All agents should appear exactly once (header + 20 agent lines)
    lines = [line for line in output.strip().split("\n") if line.strip()]
    # 1 header + 20 agent rows
    assert len(lines) == agent_count + 1


def test_streaming_renderer_custom_fields() -> None:
    """Streaming renderer should respect custom field selection."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name", "type"], is_tty=False, output=captured)
    renderer.start()
    renderer(make_test_agent_info())
    renderer.finish()

    output = captured.getvalue()
    assert "NAME" in output
    assert "TYPE" in output
    assert "generic" in output


def test_streaming_renderer_limit_caps_output() -> None:
    """Streaming renderer with limit should stop displaying agents after limit is reached."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=False, output=captured)
    renderer.limit = 2
    renderer.start()

    renderer(make_test_agent_info(name="agent-1"))
    renderer(make_test_agent_info(name="agent-2"))
    renderer(make_test_agent_info(name="agent-3"))
    renderer(make_test_agent_info(name="agent-4"))

    renderer.finish()

    output = captured.getvalue()
    lines = [line for line in output.strip().split("\n") if line.strip()]
    # 1 header + 2 agent rows (limit=2, agents 3 and 4 are dropped)
    assert len(lines) == 3
    assert "agent-1" in output
    assert "agent-2" in output
    assert "agent-3" not in output
    assert "agent-4" not in output


def test_streaming_renderer_no_limit_shows_all() -> None:
    """Streaming renderer without limit should show all agents."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=False, output=captured)
    renderer.start()

    renderer(make_test_agent_info(name="agent-1"))
    renderer(make_test_agent_info(name="agent-2"))
    renderer(make_test_agent_info(name="agent-3"))

    renderer.finish()

    output = captured.getvalue()
    lines = [line for line in output.strip().split("\n") if line.strip()]
    # 1 header + 3 agent rows
    assert len(lines) == 4


def test_streaming_renderer_tty_erases_status_on_finish() -> None:
    """TTY streaming should erase the status line on finish."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer(make_test_agent_info())
    renderer.finish()

    output = captured.getvalue()
    # The final write should end with an erase-line sequence (no trailing status)
    assert output.endswith("\r\x1b[K")


def test_streaming_renderer_warning_stays_below_new_agents() -> None:
    """Warnings should stay at the bottom when new agents arrive after the warning."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer(make_test_agent_info(name="agent-1"))
    renderer.emit_warning("WARNING: bad thing\n")
    renderer(make_test_agent_info(name="agent-2"))
    renderer.finish()

    output = captured.getvalue()
    # In the final output, the warning should be re-written after agent-2.
    assert output.rfind("WARNING: bad thing") > output.rfind("agent-2")


def test_streaming_renderer_emit_warning_non_tty() -> None:
    """Non-TTY streaming should write warnings inline without ANSI codes."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=False, output=captured)
    renderer.start()
    renderer(make_test_agent_info(name="agent-1"))
    renderer.emit_warning("WARNING: something\n")
    renderer.finish()

    output = captured.getvalue()
    assert "\x1b" not in output
    assert "WARNING: something" in output


def test_streaming_renderer_warning_before_any_agents() -> None:
    """Warning before any agents should still appear in the output."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer.emit_warning("WARNING: early warning\n")
    renderer(make_test_agent_info(name="agent-1"))
    renderer.finish()

    output = captured.getvalue()
    assert "WARNING: early warning" in output
    assert "agent-1" in output
    # Warning should be re-written after agent-1 (pinned to bottom)
    assert output.rfind("WARNING: early warning") > output.rfind("agent-1")


def test_streaming_renderer_multiple_warnings_stay_at_bottom() -> None:
    """Multiple warnings should all stay pinned at the bottom."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer(make_test_agent_info(name="agent-1"))
    renderer.emit_warning("WARNING: first\n")
    renderer.emit_warning("WARNING: second\n")
    renderer(make_test_agent_info(name="agent-2"))
    renderer.finish()

    output = captured.getvalue()
    # Both warnings should appear after agent-2 in the final output
    agent2_pos = output.rfind("agent-2")
    warning1_pos = output.rfind("WARNING: first")
    warning2_pos = output.rfind("WARNING: second")
    assert warning1_pos > agent2_pos
    assert warning2_pos > agent2_pos
    # Warnings should be in order
    assert warning2_pos > warning1_pos


def test_streaming_renderer_warnings_interleaved_with_agents() -> None:
    """Warnings separated by agents should all end up pinned at the bottom."""
    captured = StringIO()
    renderer = _create_streaming_renderer(fields=["name"], is_tty=True, output=captured)
    renderer.start()
    renderer(make_test_agent_info(name="agent-1"))
    renderer.emit_warning("WARNING: first\n")
    renderer(make_test_agent_info(name="agent-2"))
    renderer.emit_warning("WARNING: second\n")
    renderer(make_test_agent_info(name="agent-3"))
    renderer.finish()

    output = captured.getvalue()
    # Both warnings should appear after agent-3 in the final output
    agent3_pos = output.rfind("agent-3")
    warning1_pos = output.rfind("WARNING: first")
    warning2_pos = output.rfind("WARNING: second")
    assert warning1_pos > agent3_pos
    assert warning2_pos > agent3_pos
    # Warnings should be in order
    assert warning2_pos > warning1_pos


# =============================================================================
# Tests for _should_use_streaming_mode
# =============================================================================


def test_should_use_streaming_mode_default_human() -> None:
    """Default HUMAN format without watch/sort should use streaming mode."""
    assert (
        _should_use_streaming_mode(
            output_format=OutputFormat.HUMAN,
            is_watch=False,
            is_sort_explicit=False,
        )
        is True
    )


def test_should_use_streaming_mode_json_with_explicit_sort_uses_batch() -> None:
    """JSON format with explicit sort should use batch mode."""
    assert (
        _should_use_streaming_mode(
            output_format=OutputFormat.JSON,
            is_watch=False,
            is_sort_explicit=True,
        )
        is False
    )


def test_should_use_streaming_mode_with_explicit_sort_uses_batch() -> None:
    """--sort should force batch mode for sorted output."""
    assert (
        _should_use_streaming_mode(
            output_format=OutputFormat.HUMAN,
            is_watch=False,
            is_sort_explicit=True,
        )
        is False
    )


def test_should_use_streaming_mode_with_watch_uses_batch() -> None:
    """--watch should force batch mode."""
    assert (
        _should_use_streaming_mode(
            output_format=OutputFormat.HUMAN,
            is_watch=True,
            is_sort_explicit=False,
        )
        is False
    )


def test_should_use_streaming_mode_json_format_uses_batch() -> None:
    """JSON format should use batch mode."""
    assert (
        _should_use_streaming_mode(
            output_format=OutputFormat.JSON,
            is_watch=False,
            is_sort_explicit=False,
        )
        is False
    )


# =============================================================================
# Tests for _is_streaming_eligible
# =============================================================================


def test_is_streaming_eligible_all_conditions_met() -> None:
    """_is_streaming_eligible should return True when no watch, no sort."""
    assert _is_streaming_eligible(is_watch=False, is_sort_explicit=False) is True


def test_is_streaming_eligible_watch_disables() -> None:
    """_is_streaming_eligible should return False when watch is active."""
    assert _is_streaming_eligible(is_watch=True, is_sort_explicit=False) is False


def test_is_streaming_eligible_explicit_sort_disables() -> None:
    """_is_streaming_eligible should return False when sort is explicit."""
    assert _is_streaming_eligible(is_watch=False, is_sort_explicit=True) is False


# =============================================================================
# Tests for _render_format_template
# =============================================================================


def test_render_format_template_simple_field() -> None:
    """_render_format_template should expand a simple field."""
    agent = make_test_agent_info(name="my-agent")
    result = _render_format_template("{name}", agent)
    assert result == "my-agent"


def test_render_format_template_multiple_fields() -> None:
    """_render_format_template should expand multiple fields."""
    agent = make_test_agent_info(name="my-agent", state=AgentLifecycleState.RUNNING)
    result = _render_format_template("{name} {state}", agent)
    assert result == "my-agent RUNNING"


def test_render_format_template_nested_field() -> None:
    """_render_format_template should expand nested fields with dot notation."""
    agent = make_test_agent_info()
    result = _render_format_template("{host.name}", agent)
    assert result == "test-host"


def test_render_format_template_nested_host_field() -> None:
    """_render_format_template should resolve nested host fields."""
    agent = make_test_agent_info()
    result = _render_format_template("{host.provider_name}", agent)
    assert result == "local"


def test_render_format_template_unknown_field() -> None:
    """_render_format_template should resolve unknown fields to empty string."""
    agent = make_test_agent_info()
    result = _render_format_template("{nonexistent}", agent)
    assert result == ""


def test_render_format_template_format_spec() -> None:
    """_render_format_template should support format specifications."""
    agent = make_test_agent_info(name="hi")
    result = _render_format_template("{name:>10}", agent)
    assert result == "        hi"


def test_render_format_template_literal_braces() -> None:
    """_render_format_template should handle escaped braces."""
    agent = make_test_agent_info(name="my-agent")
    result = _render_format_template("{{literal}} {name}", agent)
    assert result == "{literal} my-agent"


def test_render_format_template_empty_template() -> None:
    """_render_format_template should handle empty template."""
    agent = make_test_agent_info()
    result = _render_format_template("", agent)
    assert result == ""


def test_render_format_template_literal_text_only() -> None:
    """_render_format_template should handle template with no fields."""
    agent = make_test_agent_info()
    result = _render_format_template("just text", agent)
    assert result == "just text"


def test_render_format_template_tab_separator() -> None:
    """_render_format_template should handle tab characters in templates."""
    agent = make_test_agent_info(name="my-agent", state=AgentLifecycleState.STOPPED)
    result = _render_format_template("{name}\t{state}", agent)
    assert result == "my-agent\tSTOPPED"


# =============================================================================
# Tests for _emit_template_output
# =============================================================================


def test_emit_template_output() -> None:
    """_emit_template_output should produce one line per agent, and nothing for empty list."""
    # Empty list produces no output
    empty_output = StringIO()
    _emit_template_output([], "{name}", output=empty_output)
    assert empty_output.getvalue() == ""

    # Multiple agents produce one line each
    agents = [
        make_test_agent_info(name="agent-alpha"),
        make_test_agent_info(name="agent-bravo"),
        make_test_agent_info(name="agent-charlie"),
    ]
    captured = StringIO()
    _emit_template_output(agents, "{name}", output=captured)

    lines = captured.getvalue().strip().split("\n")
    assert len(lines) == 3
    assert lines[0] == "agent-alpha"
    assert lines[1] == "agent-bravo"
    assert lines[2] == "agent-charlie"


# =============================================================================
# Tests for _StreamingTemplateEmitter
# =============================================================================


def test_streaming_template_emitter_writes_formatted_line() -> None:
    """_StreamingTemplateEmitter should write one template-expanded line per agent."""
    captured = StringIO()
    emitter = _StreamingTemplateEmitter(format_template="{name}\t{state}", output=captured)

    agent = make_test_agent_info(name="my-agent", state=AgentLifecycleState.RUNNING)
    emitter(agent)

    output = captured.getvalue()
    assert output == "my-agent\tRUNNING\n"


def test_streaming_template_emitter_multiple_agents() -> None:
    """_StreamingTemplateEmitter should write one line per agent call."""
    captured = StringIO()
    emitter = _StreamingTemplateEmitter(format_template="{name}", output=captured)

    emitter(make_test_agent_info(name="agent-one"))
    emitter(make_test_agent_info(name="agent-two"))
    emitter(make_test_agent_info(name="agent-three"))

    lines = captured.getvalue().strip().split("\n")
    assert len(lines) == 3
    assert lines[0] == "agent-one"
    assert lines[1] == "agent-two"
    assert lines[2] == "agent-three"


def test_streaming_template_emitter_limit_caps_output() -> None:
    """_StreamingTemplateEmitter with limit should stop emitting after limit is reached."""
    captured = StringIO()
    emitter = _StreamingTemplateEmitter(format_template="{name}", output=captured, limit=2)

    emitter(make_test_agent_info(name="agent-one"))
    emitter(make_test_agent_info(name="agent-two"))
    emitter(make_test_agent_info(name="agent-three"))

    lines = captured.getvalue().strip().split("\n")
    assert len(lines) == 2
    assert lines[0] == "agent-one"
    assert lines[1] == "agent-two"


def test_streaming_template_emitter_thread_safety() -> None:
    """_StreamingTemplateEmitter should handle concurrent calls without data corruption."""
    captured = StringIO()
    emitter = _StreamingTemplateEmitter(format_template="{name}", output=captured)

    agent_count = 50
    agents = [make_test_agent_info(name=f"agent-{i}") for i in range(agent_count)]

    threads = [threading.Thread(target=emitter, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    output = captured.getvalue()
    lines = [line for line in output.strip().split("\n") if line]
    assert len(lines) == agent_count


# =============================================================================
# Tests for _format_value_as_string with dict and tuple values
# =============================================================================


def test_format_value_as_string_formats_dict_as_key_value_pairs() -> None:
    """_format_value_as_string should format dicts as 'key=value' pairs."""
    result = _format_value_as_string({"project": "mng", "env": "prod"})
    assert result == "project=mng, env=prod"


def test_format_value_as_string_returns_empty_for_empty_dict() -> None:
    """_format_value_as_string should return empty string for empty dicts."""
    result = _format_value_as_string({})
    assert result == ""


def test_format_value_as_string_formats_tuple_as_comma_separated() -> None:
    """_format_value_as_string should format tuples as comma-separated values."""
    result = _format_value_as_string(("USER", "AGENT", "SSH"))
    assert result == "USER, AGENT, SSH"


# =============================================================================
# Tests for _get_header_label
# =============================================================================


def test_get_header_label_returns_custom_label_for_known_fields() -> None:
    """_get_header_label should return custom labels for configured fields."""
    assert _get_header_label("host.name") == "HOST"
    assert _get_header_label("host.provider_name") == "PROVIDER"
    assert _get_header_label("host.tags") == "TAGS"
    assert _get_header_label("labels") == "LABELS"


def test_get_header_label_returns_uppercased_field_for_unknown_fields() -> None:
    """_get_header_label should uppercase and replace dots with spaces for unknown fields."""
    assert _get_header_label("name") == "NAME"
    assert _get_header_label("some.nested.field") == "SOME NESTED FIELD"


# =============================================================================
# Tests for _get_field_value with tags
# =============================================================================


def test_get_field_value_formats_host_tags_as_key_value_pairs() -> None:
    """_get_field_value should format host.tags dict as 'key=value' pairs."""
    agent = make_test_agent_info(host_tags={"project": "mng"})
    result = _get_field_value(agent, "host.tags")
    assert result == "project=mng"


def test_get_field_value_returns_empty_for_empty_tags() -> None:
    """_get_field_value should return empty string for empty tags."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "host.tags")
    assert result == ""


# =============================================================================
# Tests for --project and --tag CLI option parsing
# =============================================================================


def test_project_option_generates_cel_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--project should filter to agents with the specified project label."""
    # Use --project with a non-existent project to verify the filter works
    # (should return no agents since no local agents have this label)
    result = cli_runner.invoke(
        list_command,
        ["--project", "nonexistent-project-849213"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_tag_option_generates_cel_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--tag should filter to agents with the specified tag key=value."""
    result = cli_runner.invoke(
        list_command,
        ["--tag", "env=nonexistent-849213"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_tag_option_rejects_invalid_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--tag should reject values not in KEY=VALUE format."""
    result = cli_runner.invoke(
        list_command,
        ["--tag", "invalid-no-equals"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


# =============================================================================
# Tests for --label CLI option parsing
# =============================================================================


def test_label_option_generates_cel_filter(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--label should filter to agents with the specified label key=value."""
    result = cli_runner.invoke(
        list_command,
        ["--label", "env=nonexistent-293847"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found" in result.output


def test_label_option_rejects_invalid_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--label should reject values not in KEY=VALUE format."""
    result = cli_runner.invoke(
        list_command,
        ["--label", "invalid-no-equals"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


# =============================================================================
# Tests for labels display in _get_field_value
# =============================================================================


def test_get_field_value_formats_labels_as_key_value_pairs() -> None:
    """_get_field_value should format labels dict as 'key=value' pairs."""
    agent = make_test_agent_info(labels={"project": "mng"})
    result = _get_field_value(agent, "labels")
    assert result == "project=mng"


def test_get_field_value_returns_empty_for_empty_labels() -> None:
    """_get_field_value should return empty string for empty labels."""
    agent = make_test_agent_info()
    result = _get_field_value(agent, "labels")
    assert result == ""


def test_get_field_value_formats_multiple_labels() -> None:
    """_get_field_value should format multiple labels as comma-separated pairs."""
    agent = make_test_agent_info(labels={"project": "mng", "env": "prod"})
    result = _get_field_value(agent, "labels")
    # Dict ordering is guaranteed in Python 3.7+ so we can check exact output
    assert "project=mng" in result
    assert "env=prod" in result


def test_get_field_value_accesses_specific_label() -> None:
    """_get_field_value should access a specific label via dot notation."""
    agent = make_test_agent_info(labels={"project": "mng", "env": "prod"})
    result = _get_field_value(agent, "labels.project")
    assert result == "mng"


def test_get_field_value_returns_empty_for_missing_label() -> None:
    """_get_field_value should return empty for a label key that does not exist."""
    agent = make_test_agent_info(labels={"project": "mng"})
    result = _get_field_value(agent, "labels.nonexistent")
    assert result == ""


# =============================================================================
# Fake api_list_agents for CLI-level list command output formatting tests
# =============================================================================


class FakeApiListAgents:
    """Replacement for api_list_agents that returns pre-built agents.

    When the list command calls api_list_agents, this callable returns a
    ListResult containing the pre-configured agents. If the caller passes
    an on_agent callback (streaming mode), agents are delivered via that
    callback as well.
    """

    def __init__(self, agents: list[AgentInfo]) -> None:
        self.agents = agents

    def __call__(self, **kwargs: Any) -> ListResult:
        result = ListResult(agents=list(self.agents))
        on_agent: Callable[[AgentInfo], None] | None = kwargs.get("on_agent")
        if on_agent is not None:
            for agent in self.agents:
                on_agent(agent)
        return result


def _patch_list_agents(monkeypatch: pytest.MonkeyPatch, agents: list[AgentInfo]) -> None:
    """Replace api_list_agents with a fake that returns the given agents."""
    monkeypatch.setattr("imbue.mng.cli.list.api_list_agents", FakeApiListAgents(agents))


# =============================================================================
# CLI-level tests: list_command output formatting with monkeypatched agents
# =============================================================================


def test_list_command_json_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format json should emit JSON with agent data."""
    agents = [
        make_test_agent_info(name="alpha", state=AgentLifecycleState.RUNNING),
        make_test_agent_info(name="bravo", state=AgentLifecycleState.STOPPED),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "json", "--sort", "name"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output["agents"]) == 2
    names = [a["name"] for a in output["agents"]]
    assert "alpha" in names
    assert "bravo" in names


def test_list_command_jsonl_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format jsonl should emit one JSON line per agent."""
    agents = [
        make_test_agent_info(name="agent-one"),
        make_test_agent_info(name="agent-two"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    parsed_names = {json.loads(line)["name"] for line in lines}
    assert parsed_names == {"agent-one", "agent-two"}


def test_list_command_human_format_table_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format human --sort name should emit a table with default fields."""
    agents = [
        make_test_agent_info(name="my-agent", state=AgentLifecycleState.RUNNING),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--sort", "name"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # Default human format includes NAME, STATE, HOST, PROVIDER headers
    assert "NAME" in result.output
    assert "STATE" in result.output
    assert "my-agent" in result.output
    assert "RUNNING" in result.output


def test_list_command_human_format_custom_fields(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --fields name,type --sort name should show only specified columns."""
    agents = [
        make_test_agent_info(name="field-test"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--fields", "name,type", "--sort", "name"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "TYPE" in result.output
    assert "field-test" in result.output
    assert "generic" in result.output
    # Should not contain default fields that were not requested
    assert "PROVIDER" not in result.output


def test_list_command_template_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format '{name}\\t{state}' should produce template-expanded lines."""
    agents = [
        make_test_agent_info(name="tpl-agent", state=AgentLifecycleState.RUNNING),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "{name}\\t{state}"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "tpl-agent\tRUNNING" in result.output


def test_list_command_json_format_with_sort(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format json --sort name --sort-order asc should sort agents."""
    agents = [
        make_test_agent_info(name="zeta"),
        make_test_agent_info(name="alpha"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "json", "--sort", "name", "--sort-order", "asc"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    names = [a["name"] for a in output["agents"]]
    assert names == ["alpha", "zeta"]


def test_list_command_json_format_with_limit(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format json --sort name --limit 1 should return only one agent."""
    agents = [
        make_test_agent_info(name="first"),
        make_test_agent_info(name="second"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "json", "--sort", "name", "--limit", "1"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output)
    assert len(output["agents"]) == 1


def test_list_command_jsonl_format_with_limit(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format jsonl --limit 1 should emit at most one JSONL line."""
    agents = [
        make_test_agent_info(name="first"),
        make_test_agent_info(name="second"),
        make_test_agent_info(name="third"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "jsonl", "--limit", "1"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 1


def test_list_command_template_format_with_sort_falls_back_to_batch(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list --format template --sort name should use batch mode and sort."""
    agents = [
        make_test_agent_info(name="zz-last"),
        make_test_agent_info(name="aa-first"),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        ["--format", "{name}", "--sort", "name", "--sort-order", "asc"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    assert len(lines) == 2
    assert lines[0] == "aa-first"
    assert lines[1] == "zz-last"


def test_list_command_human_streaming_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list (default human, no sort) should use streaming mode and show agents."""
    agents = [
        make_test_agent_info(name="stream-agent", state=AgentLifecycleState.RUNNING),
    ]
    _patch_list_agents(monkeypatch, agents)

    result = cli_runner.invoke(
        list_command,
        [],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "stream-agent" in result.output
    assert "NAME" in result.output
