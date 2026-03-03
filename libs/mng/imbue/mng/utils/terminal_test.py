import sys
from io import StringIO

from imbue.mng.utils.terminal import ANSI_DIM_GRAY
from imbue.mng.utils.terminal import ANSI_RESET
from imbue.mng.utils.terminal import StderrInterceptor
from imbue.mng.utils.terminal import ansi_visible_length
from imbue.mng.utils.terminal import count_visual_lines


def test_interceptor_routes_writes_through_callback() -> None:
    captured: list[str] = []
    interceptor = StderrInterceptor(callback=captured.append, original_stderr=StringIO())
    interceptor.write("hello")
    assert captured == ["hello"]


def test_interceptor_skips_empty_writes() -> None:
    captured: list[str] = []
    interceptor = StderrInterceptor(callback=captured.append, original_stderr=StringIO())
    interceptor.write("")
    assert captured == []


def test_interceptor_returns_length_of_input() -> None:
    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=StringIO())
    assert interceptor.write("hello") == 5
    assert interceptor.write("") == 0


class _SimulatedBrokenPipe(OSError):
    """Simulates a broken-pipe error from the underlying stream."""


def test_interceptor_falls_back_to_original_on_oserror() -> None:
    original = StringIO()

    def failing_callback(s: str) -> None:
        raise _SimulatedBrokenPipe("broken pipe")

    interceptor = StderrInterceptor(callback=failing_callback, original_stderr=original)
    interceptor.write("fallback text")
    assert "fallback text" in original.getvalue()


def test_interceptor_isatty_delegates_to_original() -> None:
    original = StringIO()
    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=original)
    assert interceptor.isatty() is False


def test_interceptor_encoding_fallback() -> None:
    """encoding falls back to 'utf-8' when the original has no encoding attribute."""

    class _NoEncoding:
        pass

    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=_NoEncoding())
    assert interceptor.encoding == "utf-8"


def test_interceptor_encoding_from_original() -> None:
    """encoding returns the original stderr's encoding when it has one."""

    class _WithEncoding:
        encoding = "ascii"

    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=_WithEncoding())
    assert interceptor.encoding == "ascii"


def test_interceptor_errors_fallback() -> None:
    """errors falls back to 'strict' when the original has no errors attribute."""

    class _NoErrors:
        pass

    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=_NoErrors())
    assert interceptor.errors == "strict"


def test_interceptor_errors_from_original() -> None:
    """errors returns the original stderr's errors when it has one."""

    class _WithErrors:
        errors = "replace"

    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=_WithErrors())
    assert interceptor.errors == "replace"


def test_interceptor_flush_is_noop() -> None:
    """flush should be a no-op and not raise."""
    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=StringIO())
    interceptor.flush()


def test_interceptor_fileno_delegates_to_original() -> None:
    """fileno should delegate to original stderr."""

    class _WithFileno:
        def fileno(self) -> int:
            return 42

        def isatty(self) -> bool:
            return False

    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=_WithFileno())
    assert interceptor.fileno() == 42


def test_interceptor_context_manager_installs_and_restores_stderr() -> None:
    """Context manager should install interceptor as sys.stderr and restore on exit."""
    original = sys.stderr
    interceptor = StderrInterceptor(callback=lambda s: None, original_stderr=original)
    with interceptor:
        assert sys.stderr is interceptor
    assert sys.stderr is original


# =============================================================================
# Tests for ansi_visible_length
# =============================================================================


def test_ansi_visible_length_plain_text() -> None:
    assert ansi_visible_length("hello world") == 11


def test_ansi_visible_length_with_ansi_codes() -> None:
    text = f"{ANSI_DIM_GRAY}Searching...{ANSI_RESET}"
    assert ansi_visible_length(text) == len("Searching...")


def test_ansi_visible_length_empty_string() -> None:
    assert ansi_visible_length("") == 0


def test_ansi_visible_length_only_ansi_codes() -> None:
    assert ansi_visible_length("\x1b[38;5;245m\x1b[0m") == 0


def test_ansi_visible_length_strips_carriage_return() -> None:
    assert ansi_visible_length("\rhello") == 5


# =============================================================================
# Tests for count_visual_lines
# =============================================================================


def test_count_visual_lines_empty_string() -> None:
    assert count_visual_lines("", 80) == 0


def test_count_visual_lines_short_line_is_one_visual_line() -> None:
    assert count_visual_lines("hello\n", 80) == 1


def test_count_visual_lines_line_wraps_to_two_visual_lines() -> None:
    """A line of 100 visible chars at width 80 should wrap to 2 visual lines."""
    text = "x" * 100 + "\n"
    assert count_visual_lines(text, 80) == 2


def test_count_visual_lines_exact_width_is_one_visual_line() -> None:
    """A line exactly terminal width should be 1 visual line (no phantom wrap)."""
    text = "x" * 80 + "\n"
    assert count_visual_lines(text, 80) == 1


def test_count_visual_lines_ansi_codes_not_counted() -> None:
    """ANSI codes should not contribute to visible length for wrapping."""
    # 10 visible chars + ANSI codes, should be 1 line at width 80
    text = f"{ANSI_DIM_GRAY}0123456789{ANSI_RESET}\n"
    assert count_visual_lines(text, 80) == 1


def test_count_visual_lines_multiple_lines() -> None:
    text = "line one\nline two\nline three\n"
    assert count_visual_lines(text, 80) == 3


def test_count_visual_lines_trailing_newline_handling() -> None:
    """Trailing newline should not add an extra visual line."""
    assert count_visual_lines("hello\n", 80) == 1
    assert count_visual_lines("hello", 80) == 1


def test_count_visual_lines_no_trailing_newline_with_wrapping() -> None:
    """A long line without trailing newline should still wrap correctly."""
    text = "x" * 100
    assert count_visual_lines(text, 80) == 2


def test_count_visual_lines_multiple_wrapping_lines() -> None:
    """Multiple lines that each wrap should sum correctly."""
    # Each line is 100 chars -> 2 visual lines each, 3 lines total = 6 visual lines
    text = ("x" * 100 + "\n") * 3
    assert count_visual_lines(text, 80) == 6
