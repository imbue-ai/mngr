import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.events import EventRecord
from imbue.mngr.cli.events import EventsCliOptions
from imbue.mngr.cli.events import _emit_event_record
from imbue.mngr.cli.events import _write_and_flush_stdout
from imbue.mngr.cli.events import events
from imbue.mngr.cli.testing import create_agent_with_events_dir


def _make_events_opts(
    target: str = "my-agent",
    sources: tuple[str, ...] = (),
    source: tuple[str, ...] = (),
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
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
        sources=sources,
        source=source,
        include=include,
        exclude=exclude,
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
    assert opts.sources == ()
    assert opts.source == ()


def test_events_cli_options_with_tail() -> None:
    opts = _make_events_opts(follow=True, tail=50)
    assert opts.follow is True
    assert opts.tail == 50


def test_events_cli_options_with_head() -> None:
    opts = _make_events_opts(head=20)
    assert opts.head == 20


def test_events_cli_options_with_sources() -> None:
    opts = _make_events_opts(sources=("messages",), source=("logs/mngr",))
    assert opts.sources == ("messages",)
    assert opts.source == ("logs/mngr",)


def test_events_cli_rejects_head_and_tail_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Verify that --head and --tail cannot be used together."""
    result = cli_runner.invoke(
        events,
        ["my-agent", "--head", "5", "--tail", "10"],
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
        ["my-agent", "--head", "5", "--follow"],
        obj=plugin_manager,
    )
    assert result.exit_code != 0
    assert "Cannot use --head with --follow" in result.output


# =============================================================================
# Output helper function tests
# =============================================================================


def test_write_and_flush_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _write_and_flush_stdout writes to stdout."""
    _write_and_flush_stdout("hello world")
    captured = capsys.readouterr()
    assert captured.out == "hello world"


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


def test_events_cli_options_with_include_and_exclude() -> None:
    """Verify the include and exclude fields can be set."""
    opts = _make_events_opts(include=('source == "messages"',), exclude=('source == "logs"',))
    assert opts.include == ('source == "messages"',)
    assert opts.exclude == ('source == "logs"',)


# =============================================================================
# Tests with real agent data
# =============================================================================


def test_events_cli_streams_all_events(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """CLI events without sources should stream all JSONL events."""
    _, events_dir = create_agent_with_events_dir(
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


def test_events_cli_filters_by_source_positional(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """CLI events with positional source args should only show matching sources."""
    _, events_dir_messages = create_agent_with_events_dir(
        local_provider.host_dir, "events-source-test", events_source="messages"
    )
    msg_event = json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_id": "msg-1", "source": "messages"})
    (events_dir_messages / "events.jsonl").write_text(msg_event + "\n")

    # Create a second source under the same agent
    logs_dir = events_dir_messages.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_event = json.dumps({"timestamp": "2026-01-02T00:00:00Z", "event_id": "log-1", "source": "logs"})
    (logs_dir / "events.jsonl").write_text(log_event + "\n")

    # Filter to only messages source
    result = cli_runner.invoke(
        events,
        ["events-source-test", "messages"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "msg-1" in result.output
    assert "log-1" not in result.output


def test_events_cli_filters_by_source_option(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """CLI events with --source option should only show matching sources."""
    _, events_dir_messages = create_agent_with_events_dir(
        local_provider.host_dir, "events-source-opt-test", events_source="messages"
    )
    msg_event = json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_id": "msg-2", "source": "messages"})
    (events_dir_messages / "events.jsonl").write_text(msg_event + "\n")

    # Create a second source under the same agent
    logs_dir = events_dir_messages.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_event = json.dumps({"timestamp": "2026-01-02T00:00:00Z", "event_id": "log-2", "source": "logs"})
    (logs_dir / "events.jsonl").write_text(log_event + "\n")

    # Filter to only messages source using --source
    result = cli_runner.invoke(
        events,
        ["events-source-opt-test", "--source", "messages"],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "msg-2" in result.output
    assert "log-2" not in result.output


def test_events_cli_source_and_include_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """CLI events should allow --source and --include to be used together."""
    _, events_dir_messages = create_agent_with_events_dir(
        local_provider.host_dir, "events-source-include-test", events_source="messages"
    )
    event1 = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "event_id": "msg-a",
            "source": "messages",
            "type": "chat",
        }
    )
    event2 = json.dumps(
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "event_id": "msg-b",
            "source": "messages",
            "type": "system",
        }
    )
    (events_dir_messages / "events.jsonl").write_text(event1 + "\n" + event2 + "\n")

    # Use both --source and --include
    result = cli_runner.invoke(
        events,
        ["events-source-include-test", "--source", "messages", "--include", 'type == "chat"'],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "msg-a" in result.output
    assert "msg-b" not in result.output


def test_events_cli_source_and_exclude_together(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_provider,
    temp_mngr_ctx,
) -> None:
    """CLI events should allow --source and --exclude to be used together."""
    _, events_dir_messages = create_agent_with_events_dir(
        local_provider.host_dir, "events-source-exclude-test", events_source="messages"
    )
    event1 = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "event_id": "msg-c",
            "source": "messages",
            "type": "chat",
        }
    )
    event2 = json.dumps(
        {
            "timestamp": "2026-01-02T00:00:00Z",
            "event_id": "msg-d",
            "source": "messages",
            "type": "system",
        }
    )
    (events_dir_messages / "events.jsonl").write_text(event1 + "\n" + event2 + "\n")

    # Use --source with --exclude to drop system events
    result = cli_runner.invoke(
        events,
        ["events-source-exclude-test", "--source", "messages", "--exclude", 'type == "system"'],
        obj=plugin_manager,
    )
    assert result.exit_code == 0
    assert "msg-c" in result.output
    assert "msg-d" not in result.output
