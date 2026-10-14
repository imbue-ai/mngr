import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from imbue.mngr.agents.jsonl_backward_scan import INITIAL_BACKWARD_SCAN_WINDOW_BYTES
from imbue.mngr.agents.jsonl_backward_scan import JsonlRecordVisitor
from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_file_backward
from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_tail_backward
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.errors import JsonlFileShrankDuringScanError
from imbue.mngr.utils.testing import allow_warnings

_PATH = Path("/host/agents/agent-1/transcript.jsonl")


class _CollectingVisitor(JsonlRecordVisitor):
    """Records every record it is shown, and is satisfied by the first one marked as a match."""

    visited: list[dict[str, Any]] = Field(default_factory=list)

    def visit_older_record(self, record: dict[str, Any]) -> None:
        self.visited.append(record)

    def is_satisfied(self) -> bool:
        return any(record.get("match") for record in self.visited)


def _line(record: dict[str, Any]) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


def _filler_lines(count: int, start_index: int = 0) -> bytes:
    return b"".join(_line({"index": start_index + i, "pad": "x" * 200}) for i in range(count))


def _host_with(content: bytes) -> InMemoryHostFileReader:
    return InMemoryHostFileReader(contents_by_path={_PATH: content})


def test_scan_finds_a_match_in_the_first_window_reading_only_that_window() -> None:
    content = _filler_lines(2000) + _line({"match": True, "value": 7}) + _filler_lines(3, start_index=5000)
    host = _host_with(content)
    visitor = _CollectingVisitor()

    result = scan_jsonl_file_backward(host, _PATH, visitor)

    assert [record.get("index") for record in visitor.visited] == [5002, 5001, 5000, None]
    assert visitor.visited[-1]["value"] == 7
    assert result.complete_lines_end_byte == len(content)
    assert result.file_size == len(content)
    assert host.content_bytes_read == INITIAL_BACKWARD_SCAN_WINDOW_BYTES


def test_scan_finds_a_match_after_several_doublings_visiting_each_line_once() -> None:
    filler_count = 3000
    content = _line({"match": True, "value": 11}) + _filler_lines(filler_count)
    host = _host_with(content)
    visitor = _CollectingVisitor()

    scan_jsonl_file_backward(host, _PATH, visitor)

    indexes = [record.get("index") for record in visitor.visited]
    assert indexes == list(reversed(range(filler_count))) + [None]
    assert visitor.visited[-1]["value"] == 11
    # A probe, the first window, and at least three doublings to reach the start of a ~640 KiB file
    assert host.tail_read_count >= 5


def test_scan_parses_a_record_that_straddles_a_window_edge() -> None:
    straddling = _line({"match": True, "big": "y" * 4000})
    tail_lines = _filler_lines(280, start_index=9000)
    # The record starts before the first window and ends inside it
    assert len(tail_lines) < INITIAL_BACKWARD_SCAN_WINDOW_BYTES < len(tail_lines) + len(straddling)
    content = _filler_lines(500) + straddling + tail_lines
    host = _host_with(content)
    visitor = _CollectingVisitor()

    scan_jsonl_file_backward(host, _PATH, visitor)

    assert visitor.visited[-1] == {"match": True, "big": "y" * 4000}
    assert [record.get("index") for record in visitor.visited[:-1]] == list(reversed(range(9000, 9280)))


def test_scan_decodes_a_multibyte_character_cut_by_a_window_edge() -> None:
    text_with_multibyte = "é中\U0001f600" * 400
    multibyte_line = _line({"match": True, "text": text_with_multibyte})
    tail_lines = _filler_lines(280, start_index=7000)
    # The first window starts on a UTF-8 continuation byte, in the middle of a character
    window_edge_offset_into_line = len(multibyte_line) - (INITIAL_BACKWARD_SCAN_WINDOW_BYTES - len(tail_lines))
    assert 0x80 <= multibyte_line[window_edge_offset_into_line] <= 0xBF
    content = _filler_lines(10) + multibyte_line + tail_lines
    host = _host_with(content)
    visitor = _CollectingVisitor()

    scan_jsonl_file_backward(host, _PATH, visitor)

    assert visitor.visited[-1] == {"match": True, "text": text_with_multibyte}


def test_scan_skips_an_unterminated_final_line() -> None:
    complete = _filler_lines(3) + _line({"match": True, "value": 1})
    content = complete + b'{"match": true, "value": 2}'
    host = _host_with(content)
    visitor = _CollectingVisitor()

    result = scan_jsonl_file_backward(host, _PATH, visitor)

    assert visitor.visited == [{"match": True, "value": 1}]
    assert result.complete_lines_end_byte == len(complete)
    assert result.file_size == len(content)


def test_scan_without_a_match_visits_every_line_and_skips_malformed_ones() -> None:
    content = _filler_lines(2) + b"not json\n\n[1, 2]\n" + _filler_lines(2, start_index=2)
    host = _host_with(content)
    visitor = _CollectingVisitor()

    with allow_warnings(match="Skipped a malformed line"):
        result = scan_jsonl_file_backward(host, _PATH, visitor)

    assert [record["index"] for record in visitor.visited] == [3, 2, 1, 0]
    assert result.complete_lines_end_byte == len(content)


def test_scan_of_an_empty_file_visits_nothing() -> None:
    host = _host_with(b"")
    visitor = _CollectingVisitor()

    result = scan_jsonl_file_backward(host, _PATH, visitor)

    assert visitor.visited == []
    assert result.complete_lines_end_byte == 0
    assert result.file_size == 0


def test_scan_of_a_missing_file_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        scan_jsonl_file_backward(InMemoryHostFileReader(), _PATH, _CollectingVisitor())


def test_tail_scan_from_an_empty_tail_at_the_end_of_the_file_reads_back_to_the_region_start() -> None:
    content = _filler_lines(4)
    host = _host_with(content)
    visitor = _CollectingVisitor()
    # What a scan holds when the file shrank to exactly its first window's start before that window was read.
    tail = host.read_file_tail_from_offset(_PATH, len(content))

    result = scan_jsonl_tail_backward(
        host=host,
        path=_PATH,
        visitor=visitor,
        region_start_byte=0,
        tail=tail,
        tail_start_byte=len(content),
    )

    assert [record["index"] for record in visitor.visited] == [3, 2, 1, 0]
    assert result.complete_lines_end_byte == len(content)


def test_tail_scan_visits_only_lines_after_the_region_start() -> None:
    old_lines = _filler_lines(5)
    new_lines = _filler_lines(2, start_index=100)
    host = _host_with(old_lines + new_lines)
    visitor = _CollectingVisitor()
    tail = host.read_file_tail_from_offset(_PATH, len(old_lines))

    result = scan_jsonl_tail_backward(
        host=host,
        path=_PATH,
        visitor=visitor,
        region_start_byte=len(old_lines),
        tail=tail,
        tail_start_byte=len(old_lines),
    )

    assert [record["index"] for record in visitor.visited] == [101, 100]
    assert result.complete_lines_end_byte == len(old_lines) + len(new_lines)


def test_tail_scan_raises_when_the_file_shrank_between_reads() -> None:
    content = _line({"match": True}) + _filler_lines(1000)
    host = _host_with(content)
    window_start_byte = len(content) - INITIAL_BACKWARD_SCAN_WINDOW_BYTES
    tail = host.read_file_tail_from_offset(_PATH, window_start_byte)
    host.contents_by_path[_PATH] = b"{}\n"

    with pytest.raises(JsonlFileShrankDuringScanError):
        scan_jsonl_tail_backward(
            host=host,
            path=_PATH,
            visitor=_CollectingVisitor(),
            region_start_byte=0,
            tail=tail,
            tail_start_byte=window_start_byte,
        )
