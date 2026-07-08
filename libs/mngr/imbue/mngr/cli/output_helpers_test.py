"""Tests for CLI output helpers."""

import json

import pytest

from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import OperatorResultPart
from imbue.mngr.cli.output_helpers import emit_error_event
from imbue.mngr.cli.output_helpers import emit_event
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import emit_info
from imbue.mngr.cli.output_helpers import emit_operator_result
from imbue.mngr.cli.output_helpers import format_size
from imbue.mngr.cli.output_helpers import on_error
from imbue.mngr.cli.output_helpers import render_format_template
from imbue.mngr.cli.output_helpers import write_event_line
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


def test_emit_event_human_format_without_message(capsys: pytest.CaptureFixture[str]) -> None:
    """emit_event with HUMAN format without message should not output."""
    emit_event("destroyed", {"other": "data"}, OutputFormat.HUMAN)
    # No exception should be raised


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
# Tests for write_event_line
# =============================================================================


def test_write_event_line_prepends_event_field(capsys: pytest.CaptureFixture[str]) -> None:
    """write_event_line should emit the payload with a leading ``event`` field."""
    write_event_line("destroyed", {"agent_id": "agent-123"})
    output = json.loads(capsys.readouterr().out.strip())
    assert output == {"event": "destroyed", "agent_id": "agent-123"}


# =============================================================================
# Tests for emit_operator_result
# =============================================================================


def test_emit_operator_result_json_merges_part_data(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON mode writes the parts' merged data as a bare object, without an ``event`` field."""
    parts = [
        OperatorResultPart(data={"created": True}, human="Made it"),
        OperatorResultPart(data={"bucket": None}, human=None),
    ]
    emit_operator_result("prepared", parts, OutputFormat.JSON)
    assert json.loads(capsys.readouterr().out.strip()) == {"created": True, "bucket": None}


def test_emit_operator_result_jsonl_tags_merged_data_with_event(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL mode tags the merged data with the event name."""
    parts = [OperatorResultPart(data={"created": True}, human="Made it")]
    emit_operator_result("prepared", parts, OutputFormat.JSONL)
    assert json.loads(capsys.readouterr().out.strip()) == {"event": "prepared", "created": True}


def test_operator_result_part_shown_always_has_human_line() -> None:
    """``shown`` keeps the human line and collects the keyword fields as data."""
    part = OperatorResultPart.shown("a line", foo=1, bar=None)
    assert part.human == "a line"
    assert part.data == {"foo": 1, "bar": None}


def test_operator_result_part_shown_if_drops_human_line_when_absent() -> None:
    """``shown_if`` keeps the data either way but drops the human line when the gate is None."""
    present = OperatorResultPart.shown_if("bucket-1", "made bucket-1", bucket="bucket-1")
    absent = OperatorResultPart.shown_if(None, "made nothing", bucket=None)
    assert present.human == "made bucket-1"
    assert absent.human is None
    assert absent.data == {"bucket": None}


def test_emit_operator_result_human_writes_lines_skipping_none(capsys: pytest.CaptureFixture[str]) -> None:
    """HUMAN mode writes each part's human line in order, skipping parts whose line is None."""
    parts = [
        OperatorResultPart(data={"a": 1}, human="line one"),
        OperatorResultPart(data={"b": None}, human=None),
        OperatorResultPart(data={"c": 3}, human="line three"),
    ]
    emit_operator_result("prepared", parts, OutputFormat.HUMAN)
    assert capsys.readouterr().out == "line one\nline three\n"


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
