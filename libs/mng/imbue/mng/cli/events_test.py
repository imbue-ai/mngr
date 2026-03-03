import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.events import EventFileEntry
from imbue.mng.cli.events import EventsCliOptions
from imbue.mng.cli.events import _emit_event_content
from imbue.mng.cli.events import _emit_event_file_list
from imbue.mng.cli.events import _write_and_flush_stdout
from imbue.mng.cli.events import events
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.primitives import OutputFormat


def _make_events_opts(
    target: str = "my-agent",
    event_filename: str | None = None,
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


def test_emit_event_file_list_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_file_list with no event files in HUMAN format."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_event_file_list([], "my-agent", output_opts)
    captured = capsys.readouterr()
    assert "No event files found for my-agent" in captured.out


def test_emit_event_file_list_human_with_files(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_file_list with event files in HUMAN format."""
    event_files = [
        EventFileEntry(name="output.log", size=1024),
        EventFileEntry(name="error.log", size=512),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_event_file_list(event_files, "my-agent", output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "Event files for my-agent" in output
    assert "output.log" in output
    assert "error.log" in output


def test_emit_event_file_list_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_file_list in JSON format."""
    event_files = [
        EventFileEntry(name="output.log", size=1024),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_event_file_list(event_files, "my-agent", output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["target"] == "my-agent"
    assert len(data["event_files"]) == 1
    assert data["event_files"][0]["name"] == "output.log"


def test_emit_event_file_list_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_event_file_list with a format template."""
    event_files = [
        EventFileEntry(name="output.log", size=1024),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _emit_event_file_list(event_files, "my-agent", output_opts)
    captured = capsys.readouterr()
    assert "output.log" in captured.out


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
