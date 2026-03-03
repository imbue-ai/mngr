import math
import re
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

ANSI_ERASE_LINE: Final[str] = "\r\x1b[K"
ANSI_ERASE_TO_END: Final[str] = "\x1b[J"
ANSI_DIM_GRAY: Final[str] = "\x1b[38;5;245m"
ANSI_RESET: Final[str] = "\x1b[0m"

_ANSI_ESCAPE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")


def ansi_cursor_up(lines: int) -> str:
    """ANSI escape sequence to move the cursor up by the given number of lines."""
    return f"\x1b[{lines}A"


@pure
def ansi_visible_length(text: str) -> int:
    """Return the visible length of text after stripping ANSI escape sequences."""
    return len(_ANSI_ESCAPE_PATTERN.sub("", text))


@pure
def count_visual_lines(text: str, terminal_width: int) -> int:
    """Count the number of visual lines text occupies at a given terminal width.

    Splits on newlines, computes ceil(visible_chars / terminal_width) per segment
    (minimum 1 per segment), and subtracts 1 if text ends with a trailing newline
    (the trailing newline moves the cursor but does not add a visual line).
    """
    if not text:
        return 0

    segments = text.split("\n")
    total = 0
    for segment in segments:
        visible_length = ansi_visible_length(segment)
        total += max(math.ceil(visible_length / terminal_width), 1) if terminal_width > 0 else 1

    # A trailing newline moves the cursor to the next line but does not itself
    # occupy a visual line (the next content will start there).
    if text.endswith("\n"):
        total -= 1

    return total


class StderrInterceptor(MutableModel):
    """Routes stderr writes through a callback function.

    Designed to be installed as sys.stderr to prevent external writes (e.g.
    loguru warnings) from interleaving with ANSI-managed output. The callback
    receives each non-empty write as a string.

    Use as a context manager to automatically install/restore sys.stderr.

    Falls back to writing directly to the original stderr if the callback
    raises OSError (e.g. broken pipe on the output stream), which avoids
    recursive writes through the interceptor.
    """

    model_config = {"arbitrary_types_allowed": True}

    callback: Callable[[str], None]
    original_stderr: Any

    def write(self, s: str, /) -> int:
        if s:
            try:
                self.callback(s)
            except OSError:
                self.original_stderr.write(s)
                self.original_stderr.flush()
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return self.original_stderr.isatty()

    def fileno(self) -> int:
        return self.original_stderr.fileno()

    def __enter__(self) -> Self:
        sys.stderr = self
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        sys.stderr = self.original_stderr

    @property
    def encoding(self) -> str:
        return getattr(self.original_stderr, "encoding", "utf-8")

    @property
    def errors(self) -> str:
        return getattr(self.original_stderr, "errors", "strict")
