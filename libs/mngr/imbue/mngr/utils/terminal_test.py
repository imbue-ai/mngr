import sys
from io import StringIO

from imbue.mngr.utils.terminal import StderrInterceptor
from imbue.mngr.utils.terminal import visual_line_count


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


def test_visual_line_count_single_short_line() -> None:
    assert visual_line_count("hello\n", terminal_width=80) == 1


def test_visual_line_count_wrapping_line() -> None:
    """A line longer than terminal width wraps to multiple visual lines."""
    assert visual_line_count("x" * 200 + "\n", terminal_width=80) == 3


def test_visual_line_count_exact_terminal_width() -> None:
    """A line exactly equal to terminal width takes 1 visual line (no wrap)."""
    assert visual_line_count("x" * 80 + "\n", terminal_width=80) == 1


def test_visual_line_count_strips_ansi_codes() -> None:
    """ANSI escape codes should not count toward visual width."""
    text = "\x1b[1;38;5;178mWARNING: short message\x1b[0m\n"
    assert visual_line_count(text, terminal_width=80) == 1


def test_visual_line_count_ansi_with_wrapping() -> None:
    """A long message with ANSI codes should wrap based on visible content only."""
    # 100 visible chars with ANSI codes on a 50-wide terminal = 2 visual lines
    text = "\x1b[1m" + "x" * 100 + "\x1b[0m\n"
    assert visual_line_count(text, terminal_width=50) == 2


def test_visual_line_count_multiple_newlines() -> None:
    """Multiple lines each count independently."""
    assert visual_line_count("line1\nline2\n", terminal_width=80) == 2


def test_visual_line_count_empty_line_between_newlines() -> None:
    """An empty line between newlines counts as 1 visual line."""
    assert visual_line_count("line1\n\nline3\n", terminal_width=80) == 3


def test_visual_line_count_no_trailing_newline() -> None:
    """Text without a trailing newline: only count complete lines."""
    assert visual_line_count("hello", terminal_width=80) == 0
    assert visual_line_count("line1\nhello", terminal_width=80) == 1
