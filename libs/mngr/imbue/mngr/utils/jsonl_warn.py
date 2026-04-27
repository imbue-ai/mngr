import json
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

_MALFORMED_LINE_LOG_TRUNCATION: Final[int] = 200


class MalformedJsonLineWarner(MutableModel):
    """Stateful JSONL line parser that surfaces mid-file corruption as a warning.

    Use one instance per logical reading session: a single file read, or an
    entire tail loop on a single file. Call parse() for every line that the
    session yields.

    A malformed line is silently buffered. The next non-empty line proves the
    buffered line was not a partial write at end-of-file, so a warning is
    emitted at that point. Any malformed line still buffered when the session
    ends is silently dropped (treated as a partial write at EOF).
    """

    source_description: str = Field(description="Human-readable source used in warning messages, e.g. a file path")

    _pending_malformed_line: str | None = PrivateAttr(default=None)

    def parse(self, line: str) -> tuple[dict[str, Any], str] | None:
        stripped = line.strip()
        if not stripped:
            return None
        self._flush_pending_warning()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            self._pending_malformed_line = stripped
            return None
        if not isinstance(data, dict):
            return None
        return data, stripped

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
