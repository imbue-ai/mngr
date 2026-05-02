import json
import threading
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MalformedJsonlLineError

_MALFORMED_LINE_LOG_TRUNCATION: Final[int] = 200


class MalformedJsonLineWarner(MutableModel):
    """Stateful JSONL line parser that surfaces mid-file corruption as a warning.

    Use one instance per logical reading session covering a single file, even if
    that session spans multiple phases or threads (e.g. an initial bulk read
    plus a tail loop). Call parse() for every line that the session yields.

    A malformed-JSON line is silently buffered. The next non-empty line proves the
    buffered line was not a partial write at end-of-file, so a warning is
    emitted at that point. Any malformed line still buffered when the session
    ends is silently dropped (treated as a partial write at EOF).

    Lines that parse as valid JSON but are not JSON objects (arrays, strings,
    numbers) are unambiguously corrupt data and raise ``MalformedJsonlLineError``
    rather than being buffered -- they cannot be "completed" by appending more
    bytes, so the partial-write hypothesis does not apply.

    parse() is safe to call from multiple threads concurrently.
    """

    source_description: str = Field(description="Human-readable source used in warning messages, e.g. a file path")

    _pending_malformed_line: str | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def parse(self, line: str) -> tuple[dict[str, Any], str] | None:
        stripped = line.strip()
        if not stripped:
            return None
        with self._lock:
            self._flush_pending_warning()
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                self._pending_malformed_line = stripped
                return None
        if not isinstance(data, dict):
            raise MalformedJsonlLineError(
                f"Malformed JSONL line in {self.source_description} is not a JSON object: "
                f"{stripped[:_MALFORMED_LINE_LOG_TRUNCATION]!r}"
            )
        return data, stripped

    def reset(self) -> None:
        """Drop any pending buffered malformed line without emitting a warning.

        Use when the underlying data stream becomes discontinuous (e.g. the
        file was rotated or truncated). Any buffered malformed line could only
        have come from the now-discarded prefix, so emitting a warning about
        it would misleadingly point at the new content. The buffered line is
        treated as a partial write at the prior EOF and silently dropped.
        """
        with self._lock:
            self._pending_malformed_line = None

    def _flush_pending_warning(self) -> None:
        pending = self._pending_malformed_line
        if pending is None:
            return
        self._pending_malformed_line = None
        truncated = pending[:_MALFORMED_LINE_LOG_TRUNCATION]
        logger.warning(
            "Skipped corrupt JSONL line in {} (followed by more data, indicating mid-file data loss): {!r}",
            self.source_description,
            truncated,
        )


@pure
def split_complete_lines(new_content: str) -> tuple[list[str], int]:
    """Split content into complete (newline-terminated) lines, holding back any partial.

    Returns (lines, bytes_consumed). bytes_consumed is the UTF-8 byte length of the
    portion up to and including the final newline; any trailing content after that
    final newline (an in-progress partial write) is left for the next read so it can
    be reconstructed once the writer flushes the rest.
    """
    last_newline = new_content.rfind("\n")
    if last_newline == -1:
        return [], 0
    complete_part = new_content[: last_newline + 1]
    lines = complete_part.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, len(complete_part.encode("utf-8"))
