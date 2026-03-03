import json
import queue as queue_mod
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.mng.api.connect import build_ssh_base_args
from imbue.mng.api.events import EventRecord
from imbue.mng.api.events import EventSourceInfo
from imbue.mng.api.events import EventsTarget
from imbue.mng.api.events import _AllEventsStreamState
from imbue.mng.api.events import _FollowState
from imbue.mng.api.events import _build_tail_args
from imbue.mng.api.events import _check_for_new_archived_events
from imbue.mng.api.events import _check_for_new_content
from imbue.mng.api.events import _discover_event_sources_via_volume
from imbue.mng.api.events import _extract_filename
from imbue.mng.api.events import _parse_discovered_files
from imbue.mng.api.events import _parse_file_listing_output
from imbue.mng.api.events import _sort_rotated_files_oldest_first
from imbue.mng.api.events import _start_tail_thread
from imbue.mng.api.events import _tail_source_thread_local
from imbue.mng.api.events import apply_head_or_tail
from imbue.mng.api.events import follow_event_file
from imbue.mng.api.events import list_event_files
from imbue.mng.api.events import parse_event_line
from imbue.mng.api.events import read_all_historical_events
from imbue.mng.api.events import read_event_content
from imbue.mng.api.events import refresh_events_target
from imbue.mng.api.events import resolve_events_target
from imbue.mng.api.events import sort_events_by_timestamp
from imbue.mng.api.events import stream_all_events
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import HostName
from imbue.mng.providers.local.volume import LocalVolume
from imbue.mng.utils.cel_utils import compile_cel_filters
from imbue.mng.utils.polling import poll_for_value


class _StopFollow(Exception):
    """Raised by test callbacks to break out of follow_event_file."""


def _capture_and_stop_after(captured: list[str], after_count: int = 1) -> Callable[[str], None]:
    """Create a callback that captures content and stops after N calls."""
    call_count = [0]

    def _callback(content: str) -> None:
        captured.append(content)
        call_count[0] += 1
        if call_count[0] >= after_count:
            raise _StopFollow()

    return _callback


@pytest.fixture
def events_volume_target(tmp_path: Path) -> tuple[EventsTarget, Path]:
    """Create an EventsTarget backed by a temp directory.

    Returns (target, events_dir) so tests can write files into the volume.
    """
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")
    return target, events_dir


# =============================================================================
# apply_head_or_tail tests
# =============================================================================


def test_apply_head_or_tail_returns_all_when_no_filter() -> None:
    content = "line1\nline2\nline3\n"
    result = apply_head_or_tail(content, head_count=None, tail_count=None)
    assert result == content


def test_apply_head_or_tail_returns_first_n_lines() -> None:
    content = "line1\nline2\nline3\nline4\n"
    result = apply_head_or_tail(content, head_count=2, tail_count=None)
    assert result == snapshot("line1\nline2\n")


def test_apply_head_or_tail_returns_last_n_lines() -> None:
    content = "line1\nline2\nline3\nline4\n"
    result = apply_head_or_tail(content, head_count=None, tail_count=2)
    assert result == snapshot("line3\nline4\n")


def test_apply_head_or_tail_handles_head_larger_than_content() -> None:
    content = "line1\nline2\n"
    result = apply_head_or_tail(content, head_count=10, tail_count=None)
    assert result == content


def test_apply_head_or_tail_handles_tail_larger_than_content() -> None:
    content = "line1\nline2\n"
    result = apply_head_or_tail(content, head_count=None, tail_count=10)
    assert result == content


def test_apply_head_or_tail_handles_empty_content() -> None:
    result = apply_head_or_tail("", head_count=5, tail_count=None)
    assert result == ""


# =============================================================================
# _extract_filename tests
# =============================================================================


def test_extract_filename_from_simple_path() -> None:
    assert _extract_filename("output.log") == "output.log"


def test_extract_filename_from_nested_path() -> None:
    assert _extract_filename("some/dir/output.log") == "output.log"


# =============================================================================
# list_event_files / read_event_content tests
# =============================================================================


def test_list_event_files_returns_only_files(events_volume_target: tuple[EventsTarget, Path]) -> None:
    target, events_dir = events_volume_target
    (events_dir / "output.log").write_text("some log data")
    (events_dir / "error.log").write_text("some errors")
    (events_dir / "subdir").mkdir()

    event_files = list_event_files(target)

    names = sorted(ef.name for ef in event_files)
    assert names == snapshot(["error.log", "output.log"])


def test_read_event_content_returns_file_contents(events_volume_target: tuple[EventsTarget, Path]) -> None:
    target, events_dir = events_volume_target
    (events_dir / "test.log").write_text("hello world\nsecond line\n")

    content = read_event_content(target, "test.log")

    assert content == snapshot("hello world\nsecond line\n")


# =============================================================================
# resolve_events_target tests
# =============================================================================


def _create_agent_data_json(
    # The per-host directory (local_provider.host_dir)
    per_host_dir: Path,
    agent_name: str,
    command: str,
) -> AgentId:
    """Create an agent data.json file so the agent appears in agent references.

    Returns the generated AgentId.
    """
    agent_id = AgentId.generate()
    agent_dir = per_host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": "generic",
        "command": command,
        "work_dir": "/tmp/test",
        "create_time": "2026-01-01T00:00:00+00:00",
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    return agent_id


def test_resolve_events_target_finds_agent(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify resolve_events_target finds an agent and returns a scoped events volume."""
    per_host_dir = local_provider.host_dir
    agent_id = _create_agent_data_json(per_host_dir, "test-resolve-agent", "sleep 94817")

    # Create events in the agent's directory (volume and host_dir are the same path now)
    agent_events_dir = per_host_dir / "agents" / str(agent_id) / "events"
    agent_events_dir.mkdir(parents=True, exist_ok=True)
    (agent_events_dir / "output.log").write_text("agent log content\n")

    # Resolve should find the agent
    target = resolve_events_target("test-resolve-agent", temp_mng_ctx)
    assert "test-resolve-agent" in target.display_name

    # Should be able to list and read event files via the online host
    event_files = list_event_files(target)
    assert len(event_files) == 1
    assert event_files[0].name == "output.log"

    content = read_event_content(target, "output.log")
    assert "agent log content" in content


def test_resolve_events_target_finds_host(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify resolve_events_target falls back to host when no agent matches."""
    per_host_dir = local_provider.host_dir
    host = local_provider.get_host(HostName("localhost"))

    # Create an agent so the host appears in load_all_agents_grouped_by_host
    _create_agent_data_json(per_host_dir, "unrelated-agent-47291", "sleep 47291")

    # Create events directly in the host volume (not under agents/)
    host_events_dir = per_host_dir / "events"
    host_events_dir.mkdir(parents=True, exist_ok=True)
    (host_events_dir / "host-output.log").write_text("host log content\n")

    # Resolve using the host ID (not name, since "local" doesn't match agent-first)
    target = resolve_events_target(str(host.id), temp_mng_ctx)
    assert "host" in target.display_name

    # Should be able to list and read event files via the online host
    event_files = list_event_files(target)
    assert len(event_files) == 1
    assert event_files[0].name == "host-output.log"

    content = read_event_content(target, "host-output.log")
    assert "host log content" in content


def test_resolve_events_target_raises_for_unknown_identifier(
    temp_mng_ctx: MngContext,
) -> None:
    with pytest.raises(UserInputError, match="No agent or host found"):
        resolve_events_target("nonexistent-identifier-abc123", temp_mng_ctx)


# =============================================================================
# _check_for_new_content tests
# =============================================================================


def test_check_for_new_content_detects_appended_content(events_volume_target: tuple[EventsTarget, Path]) -> None:
    """Verify _check_for_new_content detects new content appended to an event file."""
    target, events_dir = events_volume_target
    event_file = events_dir / "test.log"
    event_file.write_text("initial content\n")

    captured_content: list[str] = []
    state = _FollowState(previous_length=len("initial content\n"))

    # No new content yet
    _check_for_new_content(target, "test.log", captured_content.append, state)
    assert captured_content == []

    # Append new content
    event_file.write_text("initial content\nnew line\n")

    _check_for_new_content(target, "test.log", captured_content.append, state)
    assert len(captured_content) == 1
    assert captured_content[0] == "new line\n"


def test_check_for_new_content_handles_truncated_file(events_volume_target: tuple[EventsTarget, Path]) -> None:
    """Verify _check_for_new_content handles file truncation."""
    target, events_dir = events_volume_target
    event_file = events_dir / "test.log"
    event_file.write_text("long content that will be truncated\n")

    captured_content: list[str] = []
    state = _FollowState(previous_length=len("long content that will be truncated\n"))

    # Truncate the file
    event_file.write_text("short\n")

    _check_for_new_content(target, "test.log", captured_content.append, state)
    assert len(captured_content) == 1
    assert captured_content[0] == "short\n"


# =============================================================================
# follow_event_file tests
# =============================================================================


def test_follow_event_file_emits_initial_content_with_tail(events_volume_target: tuple[EventsTarget, Path]) -> None:
    """Verify follow_event_file emits tailed initial content via the callback."""
    target, events_dir = events_volume_target
    (events_dir / "test.log").write_text("line1\nline2\nline3\nline4\nline5\n")

    captured: list[str] = []

    with pytest.raises(_StopFollow):
        follow_event_file(
            target=target,
            event_file_name="test.log",
            on_new_content=_capture_and_stop_after(captured),
            tail_count=2,
        )

    assert len(captured) == 1
    assert captured[0] == "line4\nline5\n"


def test_follow_event_file_emits_all_content_when_no_tail(events_volume_target: tuple[EventsTarget, Path]) -> None:
    """Verify follow_event_file emits all content when tail_count is None."""
    target, events_dir = events_volume_target
    (events_dir / "test.log").write_text("line1\nline2\n")

    captured: list[str] = []

    with pytest.raises(_StopFollow):
        follow_event_file(
            target=target,
            event_file_name="test.log",
            on_new_content=_capture_and_stop_after(captured),
            tail_count=None,
        )

    assert len(captured) == 1
    assert captured[0] == "line1\nline2\n"


# =============================================================================
# _parse_file_listing_output tests
# =============================================================================


def test_parse_file_listing_output_parses_tab_separated_entries() -> None:
    output = "output.log\t1234\nerror.log\t567\n"
    entries = _parse_file_listing_output(output)
    assert len(entries) == 2
    assert entries[0].name == "output.log"
    assert entries[0].size == 1234
    assert entries[1].name == "error.log"
    assert entries[1].size == 567


def test_parse_file_listing_output_skips_empty_lines() -> None:
    output = "output.log\t100\n\n\nerror.log\t200\n"
    entries = _parse_file_listing_output(output)
    assert len(entries) == 2


def test_parse_file_listing_output_handles_invalid_size() -> None:
    output = "output.log\tnot_a_number\n"
    entries = _parse_file_listing_output(output)
    assert len(entries) == 1
    assert entries[0].size == 0


def test_parse_file_listing_output_handles_empty_output() -> None:
    entries = _parse_file_listing_output("")
    assert entries == []


# =============================================================================
# _build_tail_args tests
# =============================================================================


def test_build_tail_args_with_tail_count() -> None:
    args = _build_tail_args(Path("/tmp/test.log"), tail_count=50)
    assert args == snapshot(["tail", "-n", "50", "-f", "/tmp/test.log"])


def test_build_tail_args_without_tail_count_shows_from_beginning() -> None:
    args = _build_tail_args(Path("/tmp/test.log"), tail_count=None)
    assert args == snapshot(["tail", "-n", "+1", "-f", "/tmp/test.log"])


# =============================================================================
# Host-based list/read tests
# =============================================================================


@pytest.fixture
def events_host_target(
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    local_provider,
) -> tuple[EventsTarget, Path]:
    """Create an EventsTarget backed by a local online host (no volume).

    Returns (target, events_dir) so tests can write files into the events directory.
    """
    events_dir = tmp_path / "host_events"
    events_dir.mkdir()
    host = local_provider.get_host(HostName("localhost"))
    assert isinstance(host, OnlineHostInterface)
    target = EventsTarget(
        volume=None,
        online_host=host,
        events_path=events_dir,
        display_name="test-host",
    )
    return target, events_dir


def test_list_event_files_via_host_returns_files(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify list_event_files works via host execute_command when volume is None."""
    target, events_dir = events_host_target
    (events_dir / "output.log").write_text("some log data")
    (events_dir / "error.log").write_text("err")
    (events_dir / "subdir").mkdir()

    event_files = list_event_files(target)

    names = sorted(ef.name for ef in event_files)
    assert names == snapshot(["error.log", "output.log"])


def test_list_event_files_via_host_returns_correct_sizes(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify list_event_files via host returns correct file sizes."""
    target, events_dir = events_host_target
    (events_dir / "test.log").write_text("12345")

    event_files = list_event_files(target)

    assert len(event_files) == 1
    assert event_files[0].name == "test.log"
    assert event_files[0].size == 5


def test_list_event_files_via_host_returns_empty_for_nonexistent_dir(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify list_event_files via host returns empty list when events dir does not exist."""
    host = local_provider.get_host(HostName("localhost"))
    target = EventsTarget(
        volume=None,
        online_host=host,
        events_path=Path("/tmp/nonexistent-dir-events-92847"),
        display_name="test-host",
    )

    event_files = list_event_files(target)

    assert event_files == []


def test_read_event_content_via_host(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify read_event_content works via host execute_command when volume is None.

    Note: pyinfra's CommandOutput.stdout joins lines with newlines but drops
    the final trailing newline, so host-based reads may differ from volume-based
    reads in trailing whitespace.
    """
    target, events_dir = events_host_target
    (events_dir / "test.log").write_text("hello from host\nsecond line\n")

    content = read_event_content(target, "test.log")

    assert "hello from host" in content
    assert "second line" in content


def test_read_event_content_via_host_raises_for_missing_file(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify read_event_content via host raises MngError for missing files."""
    target, _events_dir = events_host_target

    with pytest.raises(MngError, match="Failed to read event file"):
        read_event_content(target, "nonexistent-file-58291.log")


def test_list_event_files_raises_when_no_volume_or_host() -> None:
    """Verify list_event_files raises MngError when neither volume nor host is available."""
    target = EventsTarget(display_name="test-empty")

    with pytest.raises(MngError, match="no volume or online host"):
        list_event_files(target)


def test_read_event_content_raises_when_no_volume_or_host() -> None:
    """Verify read_event_content raises MngError when neither volume nor host is available."""
    target = EventsTarget(display_name="test-empty")

    with pytest.raises(MngError, match="no volume or online host"):
        read_event_content(target, "test.log")


def test_follow_event_file_raises_when_no_volume_or_host() -> None:
    """Verify follow_event_file raises MngError when neither volume nor host is available."""
    target = EventsTarget(display_name="test-empty")

    with pytest.raises(MngError, match="no volume or online host"):
        follow_event_file(target, "test.log", lambda _: None, tail_count=None)


# =============================================================================
# resolve_events_target with online host tests
# =============================================================================


def test_resolve_events_target_populates_online_host_for_agent(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify resolve_events_target sets online_host and events_path when host is online."""
    per_host_dir = local_provider.host_dir
    agent_id = _create_agent_data_json(per_host_dir, "test-online-agent-82719", "sleep 82719")

    # Create events directory
    agent_events_dir = per_host_dir / "agents" / str(agent_id) / "events"
    agent_events_dir.mkdir(parents=True, exist_ok=True)
    (agent_events_dir / "output.log").write_text("test content\n")

    target = resolve_events_target("test-online-agent-82719", temp_mng_ctx)

    # Both volume and online_host should be populated for local provider
    assert target.volume is not None
    assert target.online_host is not None
    assert target.events_path is not None
    assert str(target.events_path).endswith(f"agents/{agent_id}/events")


# =============================================================================
# follow_event_file via host tests
# =============================================================================


def test_follow_event_file_via_host_streams_existing_content(
    events_host_target: tuple[EventsTarget, Path],
) -> None:
    """Verify follow_event_file uses tail -f on host and emits existing file content."""
    target, events_dir = events_host_target
    (events_dir / "test.log").write_text("line1\nline2\nline3\n")

    captured: list[str] = []

    with pytest.raises(_StopFollow):
        follow_event_file(
            target=target,
            event_file_name="test.log",
            on_new_content=_capture_and_stop_after(captured, after_count=3),
            tail_count=None,
        )

    # Should have received the file content line by line (tail -f streams line by line)
    joined = "".join(captured)
    assert "line1" in joined
    assert "line2" in joined
    assert "line3" in joined


def test_follow_event_file_via_host_with_tail_count(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify follow_event_file via host respects tail_count."""
    target, events_dir = events_host_target
    (events_dir / "test.log").write_text("line1\nline2\nline3\nline4\nline5\n")

    captured: list[str] = []

    with pytest.raises(_StopFollow):
        follow_event_file(
            target=target,
            event_file_name="test.log",
            on_new_content=_capture_and_stop_after(captured, after_count=2),
            tail_count=2,
        )

    # Should only see the last 2 lines
    joined = "".join(captured)
    assert "line4" in joined
    assert "line5" in joined
    assert "line1" not in joined


def test_follow_event_file_via_host_detects_new_content(events_host_target: tuple[EventsTarget, Path]) -> None:
    """Verify follow_event_file via host streams new content appended to the file."""
    target, events_dir = events_host_target
    event_file = events_dir / "test.log"
    event_file.write_text("initial\n")

    captured: list[str] = []
    append_event = threading.Event()

    def capture_signal_and_stop(content: str) -> None:
        captured.append(content)
        if not append_event.is_set():
            # After receiving initial content, signal the writer thread
            append_event.set()
        else:
            # After we see the appended content, stop
            raise _StopFollow()

    # Start a writer thread that waits for the signal then appends content
    def append_content() -> None:
        append_event.wait(timeout=10.0)
        with event_file.open("a") as f:
            f.write("appended\n")
            f.flush()

    writer = threading.Thread(target=append_content, daemon=True)
    writer.start()

    with pytest.raises(_StopFollow):
        follow_event_file(
            target=target,
            event_file_name="test.log",
            on_new_content=capture_signal_and_stop,
            tail_count=None,
        )

    joined = "".join(captured)
    assert "initial" in joined
    assert "appended" in joined


# =============================================================================
# build_ssh_base_args tests
# =============================================================================


def test_build_ssh_base_args_raises_when_no_known_hosts(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify build_ssh_base_args raises MngError when no known_hosts file is configured."""
    host = local_provider.get_host(HostName("localhost"))
    assert isinstance(host, OnlineHostInterface)

    # Local hosts have no ssh_known_hosts_file configured, so this should raise
    with pytest.raises(MngError, match="known_hosts"):
        build_ssh_base_args(host)


# =============================================================================
# parse_event_line tests
# =============================================================================


def test_parse_event_line_valid_json_with_all_fields() -> None:
    line = '{"timestamp":"2026-03-01T12:00:00Z","type":"test","event_id":"evt-abc123","source":"messages","message":"hello"}'
    record = parse_event_line(line, source_hint="fallback")
    assert record is not None
    assert record.timestamp == "2026-03-01T12:00:00Z"
    assert record.event_id == "evt-abc123"
    assert record.source == "messages"
    assert record.data["message"] == "hello"
    assert record.raw_line == line.strip()


def test_parse_event_line_missing_event_id_generates_hash() -> None:
    line = '{"timestamp":"2026-03-01T12:00:00Z","type":"test","source":"messages"}'
    record = parse_event_line(line, source_hint="fallback")
    assert record is not None
    assert record.event_id.startswith("hash-")
    assert len(record.event_id) > 10


def test_parse_event_line_missing_source_uses_hint() -> None:
    line = '{"timestamp":"2026-03-01T12:00:00Z","type":"test","event_id":"evt-abc"}'
    record = parse_event_line(line, source_hint="my_source")
    assert record is not None
    assert record.source == "my_source"


def test_parse_event_line_missing_timestamp_returns_none() -> None:
    line = '{"type":"test","event_id":"evt-abc","source":"messages"}'
    record = parse_event_line(line, source_hint="fallback")
    assert record is None


def test_parse_event_line_malformed_json_returns_none() -> None:
    record = parse_event_line("not json at all", source_hint="fallback")
    assert record is None


def test_parse_event_line_empty_string_returns_none() -> None:
    record = parse_event_line("", source_hint="fallback")
    assert record is None


def test_parse_event_line_whitespace_only_returns_none() -> None:
    record = parse_event_line("   \n  ", source_hint="fallback")
    assert record is None


# =============================================================================
# sort_events_by_timestamp tests
# =============================================================================


def test_sort_events_by_timestamp_orders_chronologically() -> None:
    events = [
        EventRecord(raw_line="c", timestamp="2026-03-03T00:00:00Z", event_id="c", source="s", data={}),
        EventRecord(raw_line="a", timestamp="2026-03-01T00:00:00Z", event_id="a", source="s", data={}),
        EventRecord(raw_line="b", timestamp="2026-03-02T00:00:00Z", event_id="b", source="s", data={}),
    ]
    sorted_events = sort_events_by_timestamp(events)
    assert [e.event_id for e in sorted_events] == ["a", "b", "c"]


def test_sort_events_by_timestamp_stable_for_equal_timestamps() -> None:
    events = [
        EventRecord(raw_line="x", timestamp="2026-03-01T00:00:00Z", event_id="x", source="s", data={}),
        EventRecord(raw_line="y", timestamp="2026-03-01T00:00:00Z", event_id="y", source="s", data={}),
    ]
    sorted_events = sort_events_by_timestamp(events)
    assert [e.event_id for e in sorted_events] == ["x", "y"]


# =============================================================================
# _sort_rotated_files_oldest_first tests
# =============================================================================


def test_sort_rotated_files_oldest_first() -> None:
    files = ["events.jsonl.1", "events.jsonl.3", "events.jsonl.2"]
    result = _sort_rotated_files_oldest_first(files)
    assert result == snapshot(["events.jsonl.3", "events.jsonl.2", "events.jsonl.1"])


def test_sort_rotated_files_empty_list() -> None:
    assert _sort_rotated_files_oldest_first([]) == []


def test_sort_rotated_files_ignores_non_matching() -> None:
    files = ["events.jsonl.1", "events.jsonl", "other.log"]
    result = _sort_rotated_files_oldest_first(files)
    assert result == snapshot(["events.jsonl.1"])


# =============================================================================
# _parse_discovered_files tests
# =============================================================================


def test_parse_discovered_files_groups_by_directory() -> None:
    find_output = (
        "/tmp/events/messages/events.jsonl\n/tmp/events/messages/events.jsonl.1\n/tmp/events/logs/mng/events.jsonl\n"
    )
    sources = _parse_discovered_files(find_output, "/tmp/events")
    assert len(sources) == 2
    # Sources are sorted by path
    assert sources[0].source_path == "logs/mng"
    assert sources[0].is_current_file_present is True
    assert sources[0].rotated_files == ()
    assert sources[1].source_path == "messages"
    assert sources[1].is_current_file_present is True
    assert sources[1].rotated_files == ("events.jsonl.1",)


def test_parse_discovered_files_handles_empty_output() -> None:
    sources = _parse_discovered_files("", "/tmp/events")
    assert sources == []


def test_parse_discovered_files_only_rotated_file() -> None:
    find_output = "/tmp/events/old_source/events.jsonl.1\n"
    sources = _parse_discovered_files(find_output, "/tmp/events")
    assert len(sources) == 1
    assert sources[0].is_current_file_present is False
    assert sources[0].rotated_files == ("events.jsonl.1",)


# =============================================================================
# discover_event_sources via volume tests
# =============================================================================


def test_discover_event_sources_via_volume(tmp_path: Path) -> None:
    """Verify _discover_event_sources_via_volume finds all event sources recursively."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    # Create multiple source directories
    (events_dir / "messages").mkdir()
    (events_dir / "messages" / "events.jsonl").write_text('{"timestamp":"2026-01-01T00:00:00Z"}\n')
    (events_dir / "messages" / "events.jsonl.1").write_text('{"timestamp":"2025-12-01T00:00:00Z"}\n')

    (events_dir / "logs" / "mng").mkdir(parents=True)
    (events_dir / "logs" / "mng" / "events.jsonl").write_text('{"timestamp":"2026-01-02T00:00:00Z"}\n')

    volume = LocalVolume(root_path=events_dir)
    sources = _discover_event_sources_via_volume(volume)

    assert len(sources) == 2
    source_paths = [s.source_path for s in sources]
    assert "messages" in source_paths
    assert "logs/mng" in source_paths

    messages_source = next(s for s in sources if s.source_path == "messages")
    assert messages_source.is_current_file_present is True
    assert messages_source.rotated_files == ("events.jsonl.1",)


def test_discover_event_sources_via_volume_empty_dir(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    volume = LocalVolume(root_path=events_dir)
    sources = _discover_event_sources_via_volume(volume)
    assert sources == []


# =============================================================================
# read_all_historical_events tests
# =============================================================================


def test_read_all_historical_events_merges_and_sorts(tmp_path: Path) -> None:
    """Verify events from multiple sources are merged and sorted by timestamp."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    # Source A: events at T=1 and T=3
    (events_dir / "source_a").mkdir()
    (events_dir / "source_a" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"a1","source":"source_a"}\n'
        '{"timestamp":"2026-01-03T00:00:00Z","event_id":"a3","source":"source_a"}\n'
    )

    # Source B: event at T=2
    (events_dir / "source_b").mkdir()
    (events_dir / "source_b" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"b2","source":"source_b"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    sources = [
        EventSourceInfo(source_path="source_a", rotated_files=(), is_current_file_present=True),
        EventSourceInfo(source_path="source_b", rotated_files=(), is_current_file_present=True),
    ]

    events, offsets = read_all_historical_events(target, sources, [], [])

    assert [e.event_id for e in events] == ["a1", "b2", "a3"]
    assert "source_a" in offsets
    assert "source_b" in offsets


def test_read_all_historical_events_includes_rotated_files(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    (events_dir / "src").mkdir()
    (events_dir / "src" / "events.jsonl.1").write_text(
        '{"timestamp":"2025-12-01T00:00:00Z","event_id":"old1","source":"src"}\n'
    )
    (events_dir / "src" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"new1","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    sources = [
        EventSourceInfo(source_path="src", rotated_files=("events.jsonl.1",), is_current_file_present=True),
    ]

    events, _ = read_all_historical_events(target, sources, [], [])

    assert [e.event_id for e in events] == ["old1", "new1"]


def test_read_all_historical_events_with_cel_filter(tmp_path: Path) -> None:
    """Verify CEL filter is applied to events."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    (events_dir / "messages").mkdir()
    (events_dir / "messages" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"m1","source":"messages","type":"msg"}\n'
    )
    (events_dir / "logs").mkdir()
    (events_dir / "logs" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"l1","source":"logs","type":"log"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    sources = [
        EventSourceInfo(source_path="messages", rotated_files=(), is_current_file_present=True),
        EventSourceInfo(source_path="logs", rotated_files=(), is_current_file_present=True),
    ]

    includes, excludes = compile_cel_filters(['source == "messages"'], [])
    events, _ = read_all_historical_events(target, sources, includes, excludes)

    assert len(events) == 1
    assert events[0].event_id == "m1"


# =============================================================================
# stream_all_events tests
# =============================================================================


class _StopStream(Exception):
    """Raised by test callbacks to break out of stream_all_events."""


def test_stream_all_events_emits_sorted_events_from_multiple_sources(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    (events_dir / "alpha").mkdir()
    (events_dir / "alpha" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"a1","source":"alpha"}\n'
        '{"timestamp":"2026-01-03T00:00:00Z","event_id":"a3","source":"alpha"}\n'
    )
    (events_dir / "beta").mkdir()
    (events_dir / "beta" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"b2","source":"beta"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    captured: list[str] = []

    stream_all_events(
        target=target,
        on_event=lambda e: captured.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=None,
        head_count=None,
        is_follow=False,
    )

    assert captured == ["a1", "b2", "a3"]


def test_stream_all_events_head_mode(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    (events_dir / "src").mkdir()
    (events_dir / "src" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"e1","source":"src"}\n'
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"e2","source":"src"}\n'
        '{"timestamp":"2026-01-03T00:00:00Z","event_id":"e3","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    captured: list[str] = []

    stream_all_events(
        target=target,
        on_event=lambda e: captured.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=None,
        head_count=2,
        is_follow=False,
    )

    assert captured == ["e1", "e2"]


def test_stream_all_events_tail_mode(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    (events_dir / "src").mkdir()
    (events_dir / "src" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"e1","source":"src"}\n'
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"e2","source":"src"}\n'
        '{"timestamp":"2026-01-03T00:00:00Z","event_id":"e3","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    captured: list[str] = []

    stream_all_events(
        target=target,
        on_event=lambda e: captured.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=2,
        head_count=None,
        is_follow=False,
    )

    assert captured == ["e2", "e3"]


def test_stream_all_events_deduplicates(tmp_path: Path) -> None:
    """Verify that events with the same event_id are not emitted twice."""
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    # Same event_id appears in both the rotated file and the current file
    (events_dir / "src").mkdir()
    (events_dir / "src" / "events.jsonl.1").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"dup1","source":"src"}\n'
    )
    (events_dir / "src" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"dup1","source":"src"}\n'
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"unique1","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    captured: list[str] = []

    stream_all_events(
        target=target,
        on_event=lambda e: captured.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=None,
        head_count=None,
        is_follow=False,
    )

    # dup1 should appear only once even though it's in both files
    assert captured.count("dup1") == 1
    assert "unique1" in captured


def test_stream_all_events_empty_events_dir(tmp_path: Path) -> None:
    events_dir = tmp_path / "events"
    events_dir.mkdir()

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    captured: list[str] = []

    stream_all_events(
        target=target,
        on_event=lambda e: captured.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=None,
        head_count=None,
        is_follow=False,
    )

    assert captured == []


# =============================================================================
# resolve_events_target populates new fields
# =============================================================================


def test_resolve_events_target_populates_provider_and_host_id(
    temp_mng_ctx: MngContext,
    local_provider,
) -> None:
    """Verify resolve_events_target sets provider, host_id, events_subpath for refresh capability."""
    per_host_dir = local_provider.host_dir
    agent_id = _create_agent_data_json(per_host_dir, "test-refresh-agent-93718", "sleep 93718")

    agent_events_dir = per_host_dir / "agents" / str(agent_id) / "events"
    agent_events_dir.mkdir(parents=True, exist_ok=True)
    (agent_events_dir / "messages").mkdir()
    (agent_events_dir / "messages" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"e1","source":"messages"}\n'
    )

    target = resolve_events_target("test-refresh-agent-93718", temp_mng_ctx)

    assert target.provider is not None
    assert target.host_id is not None
    assert target.events_subpath is not None


# =============================================================================
# Follow mode: pygtail tail thread tests
# =============================================================================


@pytest.mark.timeout(30)
def test_tail_source_thread_local_picks_up_new_events(tmp_path: Path) -> None:
    """Verify the pygtail-based tail thread detects new content appended to events.jsonl."""
    events_dir = tmp_path / "events" / "src"
    events_dir.mkdir(parents=True)
    events_file = events_dir / "events.jsonl"
    # Start with an empty file
    events_file.write_text("")

    offset_dir = tmp_path / "offsets"
    offset_dir.mkdir()
    event_queue: queue_mod.Queue[EventRecord | None] = queue_mod.Queue()
    stop_event = threading.Event()

    thread = threading.Thread(
        target=_tail_source_thread_local,
        args=(events_file, "src", event_queue, [], [], stop_event, offset_dir),
        daemon=True,
    )
    thread.start()

    try:
        # Wait for the thread to initialize pygtail by polling until the offset file exists
        offset_file = offset_dir / "src.offset"
        poll_for_value(
            producer=lambda: True if offset_file.exists() else None,
            timeout=5.0,
            poll_interval=0.2,
        )

        # Append an event
        with events_file.open("a") as f:
            f.write('{"timestamp":"2026-01-01T00:00:00Z","event_id":"t1","source":"src"}\n')
            f.flush()

        # Poll for the event to appear in the queue
        result, _, _ = poll_for_value(
            producer=lambda: event_queue.get_nowait() if not event_queue.empty() else None,
            timeout=15.0,
            poll_interval=0.5,
        )
        assert result is not None
        assert result.event_id == "t1"
    finally:
        stop_event.set()
        thread.join(timeout=5.0)


@pytest.mark.timeout(30)
def test_stream_all_events_follow_detects_new_content(tmp_path: Path) -> None:
    """Verify that a tail thread started by _start_tail_thread picks up newly appended events."""
    events_dir = tmp_path / "events"
    (events_dir / "src").mkdir(parents=True)
    events_file = events_dir / "src" / "events.jsonl"
    events_file.write_text('{"timestamp":"2026-01-01T00:00:00Z","event_id":"h1","source":"src"}\n')

    volume = LocalVolume(root_path=events_dir)
    host_target = EventsTarget(volume=volume, display_name="test")

    # Verify historical events are read in non-follow mode
    captured_historical: list[str] = []
    stream_all_events(
        target=host_target,
        on_event=lambda e: captured_historical.append(e.event_id),
        cel_include_filters=[],
        cel_exclude_filters=[],
        tail_count=None,
        head_count=None,
        is_follow=False,
    )
    assert "h1" in captured_historical

    # Start a tail thread and verify it picks up new content
    offset_dir = tmp_path / "offsets"
    offset_dir.mkdir()
    event_queue: queue_mod.Queue[EventRecord | None] = queue_mod.Queue()
    stop_event = threading.Event()

    thread = _start_tail_thread(
        target=host_target,
        source_path="src",
        event_queue=event_queue,
        cel_include_filters=[],
        cel_exclude_filters=[],
        stop_event=stop_event,
        offset_dir_path=offset_dir,
        initial_byte_offset=len(events_file.read_bytes()),
    )

    try:
        # Wait for the thread to initialize by polling for the offset file
        offset_file = offset_dir / "src.offset"
        poll_for_value(
            producer=lambda: True if offset_file.exists() else None,
            timeout=5.0,
            poll_interval=0.2,
        )

        # Append new content
        with events_file.open("a") as f:
            f.write('{"timestamp":"2026-01-02T00:00:00Z","event_id":"new1","source":"src"}\n')
            f.flush()

        # Poll for the new event
        result, _, _ = poll_for_value(
            producer=lambda: event_queue.get_nowait() if not event_queue.empty() else None,
            timeout=15.0,
            poll_interval=0.5,
        )
        assert result is not None
        assert result.event_id == "new1"
    finally:
        stop_event.set()
        thread.join(timeout=5.0)


# =============================================================================
# Rotation guard tests
# =============================================================================


def test_check_for_new_archived_events_finds_newly_rotated_files(tmp_path: Path) -> None:
    """Verify _check_for_new_archived_events detects rotated files that appeared after initial scan."""
    events_dir = tmp_path / "events"
    (events_dir / "src").mkdir(parents=True)
    (events_dir / "src" / "events.jsonl").write_text(
        '{"timestamp":"2026-01-02T00:00:00Z","event_id":"e2","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    # State says we know about "src" but have seen no rotated files yet
    state = _AllEventsStreamState(
        known_source_paths={"src"},
        known_rotated_files={"src": set()},
    )

    # Simulate a new rotated file appearing
    (events_dir / "src" / "events.jsonl.1").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"old1","source":"src"}\n'
    )

    new_events = _check_for_new_archived_events(target, state, [], [])

    assert len(new_events) == 1
    assert new_events[0].event_id == "old1"
    assert "events.jsonl.1" in state.known_rotated_files["src"]


def test_check_for_new_archived_events_skips_already_known(tmp_path: Path) -> None:
    """Verify _check_for_new_archived_events does not re-read already known rotated files."""
    events_dir = tmp_path / "events"
    (events_dir / "src").mkdir(parents=True)
    (events_dir / "src" / "events.jsonl").write_text("")
    (events_dir / "src" / "events.jsonl.1").write_text(
        '{"timestamp":"2026-01-01T00:00:00Z","event_id":"old1","source":"src"}\n'
    )

    volume = LocalVolume(root_path=events_dir)
    target = EventsTarget(volume=volume, display_name="test")

    # State already knows about the rotated file
    state = _AllEventsStreamState(
        known_source_paths={"src"},
        known_rotated_files={"src": {"events.jsonl.1"}},
    )

    new_events = _check_for_new_archived_events(target, state, [], [])
    assert new_events == []


# =============================================================================
# refresh_events_target tests
# =============================================================================


def test_refresh_events_target_returns_same_when_no_provider(tmp_path: Path) -> None:
    """Verify refresh_events_target is a no-op when provider info is missing."""
    volume = LocalVolume(root_path=tmp_path)
    target = EventsTarget(volume=volume, display_name="test")

    refreshed = refresh_events_target(target)
    assert refreshed is target
