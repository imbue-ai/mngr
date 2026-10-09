import json
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import JsonlFileShrankDuringScanError
from imbue.mngr.interfaces.data_types import FileTailRead
from imbue.mngr.interfaces.host import HostFileReadInterface

INITIAL_BACKWARD_SCAN_WINDOW_BYTES: Final[int] = 64 * 1024

# Past the end of any real file, so a tail read from here returns only the file's size.
_FILE_SIZE_PROBE_OFFSET: Final[int] = 2**62


class JsonlRecordVisitor(MutableModel, ABC):
    """Consumes a JSONL file's records newest-first and reports when older records no longer matter."""

    @abstractmethod
    def visit_older_record(self, record: dict[str, Any]) -> None:
        """Consume the record just older than every record visited so far."""

    @abstractmethod
    def is_satisfied(self) -> bool:
        """Whether this visitor has found everything it looks for, so the scan can stop."""


class JsonlBackwardScanResult(FrozenModel):
    """Where a backward scan's region ended and how much of it was complete lines."""

    complete_lines_end_byte: int = Field(
        description="Offset just past the newest newline-terminated line in the region (the region start if none)"
    )
    file_size: int = Field(description="Size of the file when the scan read its newest bytes")


def read_jsonl_file_size(host: HostFileReadInterface, path: Path) -> int:
    """Return a file's size without reading any of its content. Raises FileNotFoundError if it does not exist."""
    return host.read_file_tail_from_offset(path, _FILE_SIZE_PROBE_OFFSET).file_size


def scan_jsonl_file_backward(
    host: HostFileReadInterface,
    path: Path,
    visitor: JsonlRecordVisitor,
) -> JsonlBackwardScanResult:
    """Feed a JSONL file's complete lines to a visitor newest-first, reading only as far back as it needs."""
    probed_size = read_jsonl_file_size(host, path)
    first_window_start_byte = max(0, probed_size - INITIAL_BACKWARD_SCAN_WINDOW_BYTES)
    tail = host.read_file_tail_from_offset(path, first_window_start_byte)
    return scan_jsonl_tail_backward(
        host=host,
        path=path,
        visitor=visitor,
        region_start_byte=0,
        tail=tail,
        tail_start_byte=first_window_start_byte,
    )


def scan_jsonl_tail_backward(
    host: HostFileReadInterface,
    path: Path,
    visitor: JsonlRecordVisitor,
    # A line start: only lines at or after this offset are visited.
    region_start_byte: int,
    # Already read from the file: its bytes from tail_start_byte to its end.
    tail: FileTailRead,
    tail_start_byte: int,
) -> JsonlBackwardScanResult:
    """Feed the complete lines between region_start_byte and the file's end to a visitor newest-first.

    The scan starts with the given tail and, until the visitor is satisfied, reads windows that
    double in size back towards region_start_byte. Each line is parsed once, even though every
    window re-reads the bytes after it. A final line without a trailing newline may still be
    being written, so it is skipped.
    """
    region_end_byte = tail.file_size
    if region_end_byte < tail_start_byte:
        raise JsonlFileShrankDuringScanError(path)

    window_start_byte = tail_start_byte
    window = tail.content
    # Lines starting at or after this offset have been visited (or skipped as unterminated).
    unvisited_end_byte = region_end_byte
    complete_lines_end_byte: int | None = None
    is_region_exhausted = False
    while not is_region_exhausted:
        is_region_exhausted = window_start_byte <= region_start_byte
        unvisited_end_index = unvisited_end_byte - window_start_byte

        # Find where the complete lines end: everything after the last newline is unterminated
        if complete_lines_end_byte is None:
            last_newline_index = window.rfind(b"\n", 0, unvisited_end_index)
            if last_newline_index != -1:
                complete_lines_end_byte = window_start_byte + last_newline_index + 1
                if complete_lines_end_byte < region_end_byte:
                    logger.debug("Skipped the unterminated final line of {}; it may still be being written", path)
                unvisited_end_index = last_newline_index + 1
            elif is_region_exhausted:
                complete_lines_end_byte = region_start_byte
                unvisited_end_index = 0
            else:
                pass

        if complete_lines_end_byte is not None:
            is_satisfied, unvisited_end_index = _visit_window_lines_newest_first(
                window, unvisited_end_index, is_region_exhausted, path, visitor
            )
            if is_satisfied:
                return JsonlBackwardScanResult(
                    complete_lines_end_byte=complete_lines_end_byte, file_size=region_end_byte
                )
            unvisited_end_byte = window_start_byte + unvisited_end_index

        # Read the next window, twice the size of the last (and never empty, so the scan always moves back),
        # releasing this one first to bound memory
        if not is_region_exhausted:
            next_window_size = max(INITIAL_BACKWARD_SCAN_WINDOW_BYTES, 2 * (region_end_byte - window_start_byte))
            window_start_byte = max(region_start_byte, region_end_byte - next_window_size)
            window = b""
            window = _read_window(host, path, window_start_byte, unvisited_end_byte)

    return JsonlBackwardScanResult(
        complete_lines_end_byte=region_start_byte if complete_lines_end_byte is None else complete_lines_end_byte,
        file_size=region_end_byte,
    )


def _read_window(host: HostFileReadInterface, path: Path, window_start_byte: int, unvisited_end_byte: int) -> bytes:
    """Read a file from window_start_byte to its end, which must still lie at or past unvisited_end_byte."""
    window_read = host.read_file_tail_from_offset(path, window_start_byte)
    if window_read.file_size < unvisited_end_byte:
        raise JsonlFileShrankDuringScanError(path)
    return window_read.content


def _visit_window_lines_newest_first(
    window: bytes,
    # Just past the newline ending the newest line to visit.
    unvisited_end_index: int,
    # Whether the window starts at a line start, so its first piece is a whole line.
    is_window_at_region_start: bool,
    path: Path,
    visitor: JsonlRecordVisitor,
) -> tuple[bool, int]:
    """Feed a window's lines to the visitor newest-first, stopping once it is satisfied.

    Returns whether the visitor became satisfied, and the index at which the window's unvisited
    bytes now end. A window that does not start at a line start holds back its first piece,
    which may be the tail of a longer line; the next, larger window visits it whole.
    """
    line_end_index = unvisited_end_index - 1
    while line_end_index >= 0:
        line_start_index = window.rfind(b"\n", 0, line_end_index) + 1
        if line_start_index == 0 and not is_window_at_region_start:
            return False, line_end_index + 1
        record = _parse_jsonl_record(window[line_start_index:line_end_index], path)
        if record is not None:
            visitor.visit_older_record(record)
            if visitor.is_satisfied():
                return True, line_start_index
        line_end_index = line_start_index - 1
    return False, 0


def _parse_jsonl_record(line: bytes, path: Path) -> dict[str, Any] | None:
    """Decode one JSONL line into its record, or None if it is blank, malformed, or not an object."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        record = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("Skipped a malformed line in JSONL file {}: {}", path, e)
        return None
    return record if isinstance(record, dict) else None
