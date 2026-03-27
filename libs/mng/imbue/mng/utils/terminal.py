import sys
from collections.abc import Callable
from types import TracebackType
from typing import Any
from typing import Final
from typing import Self

from imbue.imbue_common.mutable_model import MutableModel

ANSI_ERASE_LINE: Final[str] = "\r\x1b[K"
ANSI_ERASE_TO_END: Final[str] = "\x1b[J"
ANSI_DIM_GRAY: Final[str] = "\x1b[38;5;245m"
ANSI_RESET: Final[str] = "\x1b[0m"


def ansi_cursor_up(lines: int) -> str:
    """ANSI escape sequence to move the cursor up by the given number of lines."""
    return f"\x1b[{lines}A"


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
