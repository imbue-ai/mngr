import re
import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.pure import pure
from imbue.imbue_common.mutable_model import MutableModel

ANSI_ERASE_LINE: Final[str] = "\r\x1b[K"
ANSI_ERASE_TO_END: Final[str] = "\x1b[J"
ANSI_DIM_GRAY: Final[str] = "\x1b[38;5;245m"
ANSI_RESET: Final[str] = "\x1b[0m"

_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def ansi_cursor_up(lines: int) -> str:
    """ANSI escape sequence to move the cursor up by the given number of lines."""
    return f"\x1b[{lines}A"


@pure
def visual_line_count(text: str, terminal_width: int) -> int:
    """Count the number of visual terminal lines a text string occupies.

    Accounts for ANSI escape sequences (which have zero visual width) and
    terminal line wrapping when a line exceeds terminal_width.

    The count represents how many lines the cursor moves down when the text
    is written, which is the value needed for cursor-up to undo the write.
    A trailing newline counts as a line break (the cursor moves to the next line).
    """
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    count = 0
    for line in stripped.split("\n")[:-1]:
        # Each segment between newlines takes at least 1 visual line,
        # plus additional lines if it wraps past the terminal width.
        if not line:
            count += 1
        else:
            count += (len(line) + terminal_width - 1) // terminal_width
    return count


class StderrInterceptor(MutableModel):
    """Routes stderr writes through a callback function.

    Designed to be installed as sys.stderr to prevent external writes (e.g.
    loguru warnings) from interleaving with ANSI-managed output. The callback
    receives each non-empty write as a string.

    Use as a context manager to automatically install/restore sys.stderr.

    Structurally compatible with TextIO (ty uses structural subtyping for
    sys.stderr assignment), so no explicit TextIO inheritance is needed.


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
