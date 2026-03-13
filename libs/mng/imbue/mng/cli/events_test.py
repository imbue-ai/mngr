import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.events import EventRecord
from imbue.mng.cli.events import EventsCliOptions
from imbue.mng.cli.events import _emit_event_content
from imbue.mng.cli.events import _emit_event_record
from imbue.mng.cli.events import _write_and_flush_stdout
from imbue.mng.cli.events import events
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import OutputFormat


def _create_agent_with_events_dir(
    per_host_dir: Path,
    agent_name: str,
    events_source: str | None = None,
) -> tuple[AgentId, Path]:
    """Create a minimal agent directory with an events subdirectory.

    Returns (agent_id, events_dir) where events_dir is ready for test files.
    If events_source is given, events_dir is per_host_dir/agents/<id>/events/<source>;
    otherwise it is per_host_dir/agents/<id>/events.
    """
    agent_id = AgentId.generate()
    agent_dir = per_host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": "generic",
        "command": "sleep 1",
        "work_dir": "/tmp/test",
        "create_time": "2026-01-01T00:00:00+00:00",
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    if events_source is not None:
        events_dir = agent_dir / "events" / events_source
    else:
        events_dir = agent_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return agent_id, events_dir


def _make_events_opts(
    target: str = "my-agent",
    event_filename: str | None = None,
    filter: str | None = None,
    follow: bool = False,
    tail: int | None = None,
    head: int | None = None,
) -> EventsCliOptions:
    return EventsCliOptions(
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
        target=target,
        event_filename=event_filename,
        filter=filter,
        follow=follow,
        tail=tail,
        head=head,
    )


def test_events_cli_options_can_be_constructed() -> None:
    """Verify the options class can be instantiated with all required fields."""
    opts = _make_events_opts()
    assert opts.target == "my-agent"
    assert opts.follow is False
    assert opts.tail is None
    assert opts.head is None
    assert opts.event_filename is None


def test_events_cli_options_with_tail() -> None:
    opts = _make_events_opts(follow=True, tail=50)
    assert opts.follow is True
    assert opts.tail == 50


def test_events_cli_options_with_head() -> None:
    opts = _make_events_opts(head=20)
    assert opts.head == 20


def test_events_cli_rejects_head_and_tail_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that --head and --tail cannot be used together."""
    result = cli_runner.invoke(
        events,
        ["my-agent", "output.log", "--head", "5", "--tail", "10"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot specify both --head and --tail" in result.output


def test_events_cli_rejects_head_with_follow(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that --head cannot be used with --follow."""
    result = cli_runner.invoke(
        events,
        ["my-agent", "output.log", "--head", "5", "--follow"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot use --head with --follow" in result.output


def test_events_cli_event_filename_does_not_conflict_with_common_log_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify the event_filename argument does not collide with the --log-file common option."""
    result = cli_runner.invoke(
        events,
        ["nonexistent-agent-xyz", "output.log", "--log-file", "/tmp/test-log-82741.log"],
        obj=plugin_manager,
    )
    # Should fail because "nonexistent-agent-xyz" doesn't exist, not because of param conflict
    assert result.exit_code != 0
    assert "nonexistent-agent-xyz" in result.output


# =============================================================================
# Output helper function tests
# =============================================================================


def test_write_and_flush_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _write_and_flush_stdout writes to stdout."""
    _write_and_flush_stdout("hello world")
    captured = capsys.readouterr()
    assert captured.out == "hello world"


def test_emit_event_content_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_content in HUMAN format."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_event_content("line 1\nline 2\n", "output.log", output_opts)
    captured = capsys.readouterr()
    assert "line 1\nline 2\n" in captured.out


def test_emit_event_content_human_adds_trailing_newline(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_content adds trailing newline if missing."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_event_content("no trailing newline", "output.log", output_opts)
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")


def test_emit_event_content_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_content in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_event_content("log content", "output.log", output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event_file"] == "output.log"
    assert data["content"] == "log content"


def test_emit_event_content_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_content in JSONL format outputs the same as JSON."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_event_content("jsonl content", "data.jsonl", output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event_file"] == "data.jsonl"
    assert data["content"] == "jsonl content"


def test_emit_event_content_human_empty_content(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_content with empty content in HUMAN format does not add newline."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_event_content("", "empty.log", output_opts)
    captured = capsys.readouterr()
    assert captured.out == ""


# =============================================================================
# _emit_event_record tests
# =============================================================================


def test_emit_event_record_writes_raw_line(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_event_record should write the raw_line to stdout."""
    record = EventRecord(
        raw_line='{"event_id": "e1", "timestamp": "2025-01-01T00:00:00Z"}\n',
        timestamp="2025-01-01T00:00:00Z",
        event_id="e1",
        source="test",
        data={"event_id": "e1"},
    )
    _emit_event_record(record)
    captured = capsys.readouterr()
    assert captured.out == '{"event_id": "e1", "timestamp": "2025-01-01T00:00:00Z"}\n'


def test_emit_event_record_appends_newline_if_missing(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_event_record should append a newline if raw_line does not end with one."""
    record = EventRecord(
        raw_line='{"event_id": "e2"}',
        timestamp="2025-01-01T00:00:00Z",
        event_id="e2",
        source="test",
        data={"event_id": "e2"},
    )
    _emit_event_record(record)
    captured = capsys.readouterr()
    assert captured.out == '{"event_id": "e2"}\n'


# =============================================================================
# Filter and streaming behavior tests
# =============================================================================


def test_events_cli_options_with_filter() -> None:
    """Verify the filter field can be set."""
    opts = _make_events_opts(filter='source == "messages"')
    assert opts.filter == 'source == "messages"'


def test_events_cli_rejects_filter_with_event_filename(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that --filter cannot be used with a specific event file."""
    result = cli_runner.invoke(
        events,
        ["my-agent", "output.log", "--filter", 'source == "x"'],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot use --filter with a specific event file" in result.output


# =============================================================================
# Tests with real agent data
# =============================================================================


def test_events_cli_reads_specific_event_file(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mng_ctx,
) -> None:
    """CLI events with an event file name should read and display the file."""
    _, events_dir = _create_agent_with_events_dir(local_provider.host_dir, "events-cli-test-agent")
    (events_dir / "test.log").write_text("line1\nline2\nline3\n")

    result = cli_runner.invoke(
        events,
        ["events-cli-test-agent", "test.log"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "line1" in result.output
    assert "line2" in result.output
    assert "line3" in result.output


def test_events_cli_reads_specific_event_file_with_head(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mng_ctx,
) -> None:
    """CLI events with --head should only show first N lines."""
    _, events_dir = _create_agent_with_events_dir(local_provider.host_dir, "events-head-test")
    (events_dir / "test.log").write_text("line1\nline2\nline3\nline4\nline5\n")

    result = cli_runner.invoke(
        events,
        ["events-head-test", "test.log", "--head", "2"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "line1" in result.output
    assert "line2" in result.output
    assert "line5" not in result.output


def test_events_cli_reads_specific_event_file_with_tail(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mng_ctx,
) -> None:
    """CLI events with --tail should only show last N lines."""
    _, events_dir = _create_agent_with_events_dir(local_provider.host_dir, "events-tail-test")
    (events_dir / "test.log").write_text("line1\nline2\nline3\nline4\nline5\n")

    result = cli_runner.invoke(
        events,
        ["events-tail-test", "test.log", "--tail", "2"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "line4" in result.output
    assert "line5" in result.output
    assert "line1" not in result.output


def test_events_cli_reads_specific_event_file_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mng_ctx,
) -> None:
    """CLI events with --format json should output JSON."""
    _, events_dir = _create_agent_with_events_dir(local_provider.host_dir, "events-json-test")
    (events_dir / "test.log").write_text("event data\n")

    result = cli_runner.invoke(
        events,
        ["events-json-test", "test.log", "--format", "json"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    output = json.loads(result.output.strip())
    assert "content" in output
    assert "event_file" in output


def test_events_cli_streams_all_events(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mng_ctx,
) -> None:
    """CLI events without a file name should stream all JSONL events."""
    _, events_dir = _create_agent_with_events_dir(
        local_provider.host_dir, "events-stream-test", events_source="messages"
    )
    event_line = json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_id": "evt-1", "source": "messages"})
    (events_dir / "events.jsonl").write_text(event_line + "\n")

    result = cli_runner.invoke(
        events,
        ["events-stream-test"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "evt-1" in result.output
