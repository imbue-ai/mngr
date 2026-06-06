"""Tests for CLI output helpers."""

import json

import pytest

from imbue.imbue_common.errors import SwitchError
from imbue.mngr.api.rsync import RsyncResult
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_error_event
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import on_error
from imbue.mngr.cli.output_helpers import output_git_pull_success
from imbue.mngr.cli.output_helpers import output_git_push_success
from imbue.mngr.cli.output_helpers import output_rsync_result
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import OutputFormat

# =============================================================================
# Tests for AbortError
# =============================================================================


def test_abort_error_stores_message() -> None:
    """AbortError should store the message."""
    error = AbortError("test message")
    assert error.message == "test message"
    assert str(error) == "test message"


def test_abort_error_stores_original_exception() -> None:
    """AbortError should store the original exception."""
    original = ValueError("original error")
    error = AbortError("test message", original_exception=original)
    assert error.original_exception is original


def test_abort_error_is_base_exception() -> None:
    """AbortError should be a BaseException."""
    error = AbortError("test")
    assert isinstance(error, BaseException)
    assert not isinstance(error, Exception)


# =============================================================================
# Tests for emit_info
# =============================================================================


def test_emit_info_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_info with HUMAN format should output to stdout."""
    emit_info("test message", OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "test message" in captured.out


def test_emit_info_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_info with JSONL format should output JSON line."""
    emit_info("test message", OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "info"
    assert output["message"] == "test message"


def test_emit_info_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_info with JSON format should be silent."""
    emit_info("test message", OutputFormat.JSON)
    captured = capsys.readouterr()
    assert captured.out == ""


# =============================================================================
# Tests for emit_event
# =============================================================================


def test_emit_event_human_format_with_message(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_event with HUMAN format should output message to stdout."""
    emit_event("destroyed", {"message": "Agent destroyed"}, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Agent destroyed" in captured.out


def test_emit_event_human_format_without_message() -> None:
    """emit_event with HUMAN format and no message should raise rather than silently emit nothing."""
    with pytest.raises(SwitchError):
        emit_event("destroyed", {"other": "data"}, OutputFormat.HUMAN)


def test_emit_event_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_event with JSONL format should output JSON line with event type."""
    emit_event("destroyed", {"agent_id": "agent-123"}, OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "destroyed"
    assert output["agent_id"] == "agent-123"


def test_emit_event_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_event with JSON format should be silent."""
    emit_event("destroyed", {"agent_id": "agent-123"}, OutputFormat.JSON)
    captured = capsys.readouterr()
    assert captured.out == ""


# =============================================================================
# Tests for on_error
# =============================================================================


@pytest.mark.allow_warnings(match=r"^test error")
def test_on_error_human_format_continue() -> None:
    """on_error with HUMAN format and CONTINUE should not raise."""
    # Should not raise
    on_error("test error", ErrorBehavior.CONTINUE, OutputFormat.HUMAN)


@pytest.mark.allow_warnings(match=r"^test error")
def test_on_error_human_format_abort() -> None:
    """on_error with HUMAN format and ABORT should raise AbortError."""
    with pytest.raises(AbortError) as exc_info:
        on_error("test error", ErrorBehavior.ABORT, OutputFormat.HUMAN)
    assert exc_info.value.message == "test error"


def test_on_error_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """on_error with JSONL format should output error event."""
    on_error("test error", ErrorBehavior.CONTINUE, OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "error"
    assert output["message"] == "test error"


def test_on_error_json_format_continue(capsys: pytest.CaptureFixture[str]) -> None:
    """on_error with JSON format and CONTINUE should be silent."""
    on_error("test error", ErrorBehavior.CONTINUE, OutputFormat.JSON)
    captured = capsys.readouterr()
    assert captured.out == ""


@pytest.mark.allow_warnings(match=r"^test error")
def test_on_error_stores_original_exception() -> None:
    """on_error with ABORT should include original exception in AbortError."""
    original = ValueError("original")
    with pytest.raises(AbortError) as exc_info:
        on_error("test error", ErrorBehavior.ABORT, OutputFormat.HUMAN, exc=original)
    assert exc_info.value.original_exception is original


# =============================================================================
# Tests for format_size
# =============================================================================


def test_format_size_bytes() -> None:
    """format_size should format small sizes in bytes."""
    assert format_size(0) == "0 B"
    assert format_size(1) == "1 B"
    assert format_size(100) == "100 B"
    assert format_size(512) == "512 B"
    assert format_size(1023) == "1023 B"


def test_format_size_kilobytes() -> None:
    """format_size should format sizes in kilobytes."""
    assert format_size(1024) == "1.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(10240) == "10.0 KB"
    assert format_size(1024 * 500) == "500.0 KB"
    assert format_size(1024 * 1024 - 1) == "1024.0 KB"


def test_format_size_megabytes() -> None:
    """format_size should format sizes in megabytes."""
    assert format_size(1024**2) == "1.0 MB"
    assert format_size(int(1.5 * 1024**2)) == "1.5 MB"
    assert format_size(100 * 1024**2) == "100.0 MB"


def test_format_size_gigabytes() -> None:
    """format_size should format sizes in gigabytes with two decimal places."""
    assert format_size(1024**3) == "1.00 GB"
    assert format_size(int(1.5 * 1024**3)) == "1.50 GB"
    assert format_size(10 * 1024**3) == "10.00 GB"


def test_format_size_terabytes() -> None:
    """format_size should format sizes in terabytes with two decimal places."""
    assert format_size(1024**4) == "1.00 TB"
    assert format_size(int(2.5 * 1024**4)) == "2.50 TB"


# =============================================================================
# Tests for MngrError.format_message
# =============================================================================


def test_mngr_error_format_message_without_help_text() -> None:
    """MngrError.format_message should format error message without help text."""
    error = MngrError("Something went wrong")
    result = error.format_message()
    # No "Error:" prefix - Click adds that when displaying MngrError exceptions
    assert result == "Something went wrong"


def test_mngr_error_format_message_with_help_text() -> None:
    """MngrError.format_message should include help text when provided."""
    error = MngrError("Agent not found")
    error.user_help_text = "Use 'mngr list' to see available agents."
    result = error.format_message()
    # No "Error:" prefix - Click adds that when displaying MngrError exceptions
    assert "Agent not found" in result
    assert "Use 'mngr list' to see available agents." in result


def test_mngr_error_format_message_with_multiline_help_text() -> None:
    """MngrError.format_message should handle multiline help text."""
    error = MngrError("Test error")
    error.user_help_text = "Line 1\nLine 2"
    result = error.format_message()
    # No "Error:" prefix - Click adds that when displaying MngrError exceptions
    assert "Test error" in result
    assert "Line 1\nLine 2" in result


# =============================================================================
# Tests for render_format_template
# =============================================================================


def test_render_format_template_simple_field() -> None:
    """render_format_template should substitute a single field."""
    result = render_format_template("{name}", {"name": "my-agent"})
    assert result == "my-agent"


def test_render_format_template_multiple_fields() -> None:
    """render_format_template should substitute multiple fields."""
    result = render_format_template("{name}\t{state}", {"name": "my-agent", "state": "RUNNING"})
    assert result == "my-agent\tRUNNING"


def test_render_format_template_unknown_field_returns_empty() -> None:
    """render_format_template should return empty string for unknown fields."""
    result = render_format_template("{name}\t{missing}", {"name": "my-agent"})
    assert result == "my-agent\t"


def test_render_format_template_literal_text_preserved() -> None:
    """render_format_template should preserve literal text around fields."""
    result = render_format_template("Agent: {name} is {state}!", {"name": "foo", "state": "OK"})
    assert result == "Agent: foo is OK!"


def test_render_format_template_no_fields() -> None:
    """render_format_template should handle template with no fields."""
    result = render_format_template("just text", {})
    assert result == "just text"


def test_render_format_template_conversion_s() -> None:
    """render_format_template should apply !s conversion."""
    result = render_format_template("{name!s}", {"name": "test"})
    assert result == "test"


def test_render_format_template_conversion_r() -> None:
    """render_format_template should apply !r conversion."""
    result = render_format_template("{name!r}", {"name": "test"})
    assert result == "'test'"


def test_render_format_template_conversion_a() -> None:
    """render_format_template should apply !a conversion."""
    result = render_format_template("{name!a}", {"name": "test"})
    assert result == "'test'"


def test_render_format_template_format_spec() -> None:
    """render_format_template should apply format specs."""
    result = render_format_template("{name:>10}", {"name": "test"})
    assert result == "      test"


def test_render_format_template_format_spec_left_align() -> None:
    """render_format_template should apply left-alignment format spec."""
    result = render_format_template("{name:<10}", {"name": "test"})
    assert result == "test      "


# =============================================================================
# Tests for emit_format_template_lines
# =============================================================================


def test_emit_format_template_lines_outputs_one_line_per_item(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_format_template_lines should output one line per item."""
    items = [
        {"name": "agent-1", "state": "RUNNING"},
        {"name": "agent-2", "state": "STOPPED"},
    ]
    emit_format_template_lines("{name}\t{state}", items)
    captured = capsys.readouterr()
    lines = captured.out.strip().split("\n")
    assert len(lines) == 2
    assert lines[0] == "agent-1\tRUNNING"
    assert lines[1] == "agent-2\tSTOPPED"


def test_emit_format_template_lines_empty_list(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_format_template_lines should produce no output for empty list."""
    emit_format_template_lines("{name}", [])
    captured = capsys.readouterr()
    assert captured.out == ""


# =============================================================================
# Tests for output_rsync_result
# =============================================================================


def test_output_rsync_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    result = RsyncResult(
        files_transferred=5,
        bytes_transferred=1024,
        source_path="/src",
        destination_path="/dst",
    )
    output_rsync_result(result, OutputFormat.JSON)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["files_transferred"] == 5
    assert output["bytes_transferred"] == 1024


def test_output_rsync_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    result = RsyncResult(
        files_transferred=3,
        bytes_transferred=512,
        source_path="/src",
        destination_path="/dst",
    )
    output_rsync_result(result, OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "rsync_complete"


def test_output_rsync_result_human(capsys: pytest.CaptureFixture[str]) -> None:
    result = RsyncResult(
        files_transferred=5,
        bytes_transferred=1024,
        source_path="/src",
        destination_path="/dst",
    )
    output_rsync_result(result, OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Rsync complete" in captured.out
    assert "5 files" in captured.out


# =============================================================================
# Tests for output_git_push_success and output_git_pull_success
# =============================================================================


def test_output_git_push_success_json(capsys: pytest.CaptureFixture[str]) -> None:
    output_git_push_success(OutputFormat.JSON)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["success"] is True


def test_output_git_pull_success_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    output_git_pull_success(OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "git_pull_complete"


def test_output_git_push_success_human(capsys: pytest.CaptureFixture[str]) -> None:
    output_git_push_success(OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Git push complete" in captured.out


def test_output_git_pull_success_human(capsys: pytest.CaptureFixture[str]) -> None:
    output_git_pull_success(OutputFormat.HUMAN)
    captured = capsys.readouterr()
    assert "Git pull complete" in captured.out


# =============================================================================
# Tests for write_human_line
# =============================================================================


def test_write_human_line_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    """write_human_line should write plain message without args."""
    write_human_line("Hello world")
    captured = capsys.readouterr()
    assert captured.out == "Hello world\n"


def test_write_human_line_with_args(capsys: pytest.CaptureFixture[str]) -> None:
    """write_human_line should format message with positional args."""
    write_human_line("Created {} agent(s) on {}", 3, "modal")
    captured = capsys.readouterr()
    assert captured.out == "Created 3 agent(s) on modal\n"


# =============================================================================
# Tests for write_json_line
# =============================================================================


def test_write_json_line_outputs_json(capsys: pytest.CaptureFixture[str]) -> None:
    """write_json_line should output valid JSON followed by newline."""
    write_json_line({"key": "value", "number": 42})
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["key"] == "value"
    assert output["number"] == 42


def test_write_json_line_terminates_with_newline(capsys: pytest.CaptureFixture[str]) -> None:
    """write_json_line should terminate output with newline."""
    write_json_line({"a": 1})
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")


# =============================================================================
# Tests for output_git_push_success (JSONL event name)
# =============================================================================


def test_output_git_push_success_jsonl_event_name(capsys: pytest.CaptureFixture[str]) -> None:
    output_git_push_success(OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "git_push_complete"


# =============================================================================
# Tests for on_error (JSON ABORT)
# =============================================================================


def test_on_error_json_format_abort() -> None:
    """on_error with JSON format and ABORT should raise AbortError."""
    with pytest.raises(AbortError) as exc_info:
        on_error("json error", ErrorBehavior.ABORT, OutputFormat.JSON)
    assert exc_info.value.message == "json error"


def test_on_error_jsonl_format_abort(capsys: pytest.CaptureFixture[str]) -> None:
    """on_error with JSONL format and ABORT should emit error and then raise."""
    with pytest.raises(AbortError):
        on_error("jsonl error", ErrorBehavior.ABORT, OutputFormat.JSONL)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "error"
    assert output["message"] == "jsonl error"


def test_on_error_jsonl_includes_error_class_when_exc_given(capsys: pytest.CaptureFixture[str]) -> None:
    """on_error attaches the exception's class name to the JSONL error event."""
    on_error("boom", ErrorBehavior.CONTINUE, OutputFormat.JSONL, exc=MngrError("boom"))
    output = json.loads(capsys.readouterr().out.strip())
    assert output["event"] == "error"
    assert output["error_class"] == "MngrError"


# =============================================================================
# Tests for emit_error_event
# =============================================================================


def test_emit_error_event_jsonl_emits_event_with_error_class(capsys: pytest.CaptureFixture[str]) -> None:
    """In JSONL mode, emit a structured error record carrying the exception class name."""
    emit_error_event(MngrError("no exact match"), OutputFormat.JSONL)
    output = json.loads(capsys.readouterr().out.strip())
    assert output == {"event": "error", "error_class": "MngrError", "message": "no exact match"}


def test_emit_error_event_human_format_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode must not emit a JSONL error record."""
    emit_error_event(MngrError("boom"), OutputFormat.HUMAN)
    assert capsys.readouterr().out == ""


def test_emit_error_event_none_format_is_noop(capsys: pytest.CaptureFixture[str]) -> None:
    """A missing (None) output format -- e.g. failure before option parsing -- is a no-op."""
    emit_error_event(MngrError("boom"), None)
    assert capsys.readouterr().out == ""
