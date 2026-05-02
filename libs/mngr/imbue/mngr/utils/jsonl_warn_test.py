"""Unit tests for the jsonl_warn module."""

from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.jsonl_warn import split_complete_lines
from imbue.mngr.utils.testing import capture_loguru


def test_parse_returns_data_and_stripped_for_valid_json() -> None:
    warner = MalformedJsonLineWarner(source_description="test source")
    parsed = warner.parse('{"a": 1, "b": "x"}')
    assert parsed is not None
    data, stripped = parsed
    assert data == {"a": 1, "b": "x"}
    assert stripped == '{"a": 1, "b": "x"}'


def test_parse_strips_whitespace_around_line() -> None:
    warner = MalformedJsonLineWarner(source_description="test source")
    parsed = warner.parse('  {"a": 1}  \n')
    assert parsed is not None
    _data, stripped = parsed
    assert stripped == '{"a": 1}'


def test_parse_returns_none_for_empty_line() -> None:
    warner = MalformedJsonLineWarner(source_description="test source")
    assert warner.parse("") is None
    assert warner.parse("   ") is None
    assert warner.parse("\n") is None


def test_parse_returns_none_for_non_dict_json() -> None:
    warner = MalformedJsonLineWarner(source_description="test source")
    assert warner.parse("[1, 2, 3]") is None
    assert warner.parse('"just a string"') is None
    assert warner.parse("42") is None


def test_malformed_line_followed_by_valid_emits_warning() -> None:
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse("not json") is None
        # A subsequent valid line proves the malformed one was not a partial write at EOF
        parsed = warner.parse('{"a": 1}')
        assert parsed is not None
    output = log_output.getvalue()
    assert "Skipped corrupt JSONL line in my-file" in output
    assert "not json" in output


def test_malformed_line_at_eof_does_not_warn() -> None:
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse('{"a": 1}') is not None
        # Last line is malformed; nothing else follows. Treated as partial write at EOF.
        assert warner.parse("incomplete{") is None
    assert log_output.getvalue() == ""


def test_malformed_line_followed_by_blank_line_does_not_warn_yet() -> None:
    """A trailing blank line after a malformed line should not flush the buffered warning.

    Blank lines are common in JSONL trailing whitespace and don't prove that
    real data follows the malformed line.
    """
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse("malformed{") is None
        assert warner.parse("") is None
        assert warner.parse("   ") is None
    assert log_output.getvalue() == ""


def test_consecutive_malformed_lines_emit_warning_for_all_but_last() -> None:
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse("first malformed") is None
        # The second malformed line proves the first was not at EOF -- warn for first
        assert warner.parse("second malformed") is None
        # The third malformed line proves the second was not at EOF -- warn for second
        assert warner.parse("third malformed") is None
        # No further line: the third malformed stays buffered, treated as EOF.
    output = log_output.getvalue()
    assert "first malformed" in output
    assert "second malformed" in output
    assert "third malformed" not in output


def test_long_malformed_line_is_truncated_in_warning() -> None:
    warner = MalformedJsonLineWarner(source_description="my-file")
    long_line = "x" * 5000
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse(long_line) is None
        assert warner.parse('{"a": 1}') is not None
    output = log_output.getvalue()
    # Truncated to 200 chars; full 5000-char string should not appear
    assert "x" * 200 in output
    assert "x" * 1000 not in output


def test_source_description_appears_in_warning() -> None:
    warner = MalformedJsonLineWarner(source_description="events file 'foo/bar.jsonl'")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse("malformed") is None
        assert warner.parse('{"a": 1}') is not None
    assert "events file 'foo/bar.jsonl'" in log_output.getvalue()


def test_valid_lines_only_emit_no_warnings() -> None:
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        assert warner.parse('{"a": 1}') is not None
        assert warner.parse('{"b": 2}') is not None
        assert warner.parse('{"c": 3}') is not None
    assert log_output.getvalue() == ""


def test_reset_drops_pending_malformed_line_silently() -> None:
    """reset() must drop any buffered malformed line without emitting a warning."""
    warner = MalformedJsonLineWarner(source_description="my-file")
    with capture_loguru(level="WARNING") as log_output:
        # Buffer a malformed line.
        assert warner.parse("malformed{") is None
        # Reset before any subsequent line could flush it -- simulates the data
        # stream becoming discontinuous (e.g. file rotated/truncated).
        warner.reset()
        # A subsequent valid line must NOT produce a warning about the dropped line.
        assert warner.parse('{"a": 1}') is not None
    assert log_output.getvalue() == ""


def test_reset_does_not_disable_future_corruption_detection() -> None:
    """After reset(), the warner must still detect new mid-file corruption."""
    warner = MalformedJsonLineWarner(source_description="my-file")
    warner.parse("first malformed")
    warner.reset()

    with capture_loguru(level="WARNING") as log_output:
        # New malformed line followed by a valid line should still warn.
        assert warner.parse("second malformed") is None
        assert warner.parse('{"a": 1}') is not None
    output = log_output.getvalue()
    assert "second malformed" in output
    assert "first malformed" not in output


# =============================================================================
# split_complete_lines tests
# =============================================================================


def test_split_complete_lines_returns_complete_lines_and_consumed_bytes() -> None:
    lines, consumed = split_complete_lines("line1\nline2\n")
    assert lines == ["line1", "line2"]
    assert consumed == len("line1\nline2\n".encode("utf-8"))


def test_split_complete_lines_holds_back_partial_last_line() -> None:
    lines, consumed = split_complete_lines("line1\npartial")
    assert lines == ["line1"]
    assert consumed == len("line1\n".encode("utf-8"))


def test_split_complete_lines_returns_nothing_when_no_newline() -> None:
    lines, consumed = split_complete_lines("partial-only")
    assert lines == []
    assert consumed == 0


def test_split_complete_lines_returns_nothing_for_empty_input() -> None:
    lines, consumed = split_complete_lines("")
    assert lines == []
    assert consumed == 0


def test_split_complete_lines_handles_only_blank_lines() -> None:
    lines, consumed = split_complete_lines("\n\n")
    assert lines == ["", ""]
    assert consumed == 2


def test_split_complete_lines_byte_count_matches_for_multibyte_content() -> None:
    # "café" is 5 bytes in UTF-8
    lines, consumed = split_complete_lines("café\n")
    assert lines == ["café"]
    assert consumed == len("café\n".encode("utf-8"))
