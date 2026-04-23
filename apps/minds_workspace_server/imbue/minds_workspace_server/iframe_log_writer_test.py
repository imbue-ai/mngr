"""Unit tests for the iframe-log writer."""

import json
import re
from pathlib import Path
from typing import Any

from imbue.minds_workspace_server.iframe_log_writer import IframeLogWriter

_FIXED_TIMESTAMP = "2026-04-23T12:00:00.000000000Z"
# Pattern matching the timestamped suffix produced by generate_rotation_timestamp.
_ROTATED_SUFFIX_PATTERN = re.compile(r"events\.jsonl\.\d{20}")


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _find_rotated_siblings(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if _ROTATED_SUFFIX_PATTERN.fullmatch(p.name))


def test_write_records_creates_file_and_envelope(tmp_path: Path) -> None:
    """Each record is wrapped in the envelope and appended as JSONL."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path)
    records = [
        {
            "level": "error",
            "message": "oops",
            "frame_url": "http://agent-abc.localhost:8420/service/web/foo",
            "source_id": "http://agent-abc.localhost:8420/service/web/bundle.js",
            "line": 12,
            "service_name": "web",
            "mind_id": "agent-abc",
        },
    ]
    count = writer.write_records(records, now_iso=_FIXED_TIMESTAMP)
    writer.close()
    assert count == 1
    lines = _read_lines(path)
    assert len(lines) == 1
    record = lines[0]
    assert record["timestamp"] == _FIXED_TIMESTAMP
    assert record["type"] == "electron"
    assert record["source"] == "electron/renderer/service/web/agent-abc"
    assert record["event_id"].startswith("evt-")
    assert record["level"] == "error"
    assert record["message"] == "oops"
    assert record["frame_url"].endswith("/service/web/foo")
    assert record["line"] == 12


def test_write_records_empty_list_is_noop(tmp_path: Path) -> None:
    """An empty batch neither creates the file nor errors."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path)
    count = writer.write_records([])
    writer.close()
    assert count == 0
    assert not path.exists()


def test_write_records_appends_across_calls(tmp_path: Path) -> None:
    """Successive batches append to the same file."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path)
    writer.write_records([{"level": "info", "message": "first", "service_name": "web", "mind_id": "m1"}])
    writer.write_records([{"level": "info", "message": "second", "service_name": "web", "mind_id": "m1"}])
    writer.close()
    messages = [line["message"] for line in _read_lines(path)]
    assert messages == ["first", "second"]


def test_write_records_rotates_at_max_size(tmp_path: Path) -> None:
    """When the file exceeds max_size_bytes, it is rotated with a timestamp suffix."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path, max_size_bytes=64)
    writer.write_records([{"level": "info", "message": "a" * 80, "service_name": "web", "mind_id": "m1"}])
    # The previous write pushed size above the cap; this next write triggers
    # rotation before appending.
    writer.write_records([{"level": "info", "message": "short", "service_name": "web", "mind_id": "m1"}])
    writer.close()
    rotated = _find_rotated_siblings(tmp_path)
    assert len(rotated) == 1
    assert path.exists()
    assert len(_read_lines(rotated[0])) == 1
    assert len(_read_lines(path)) == 1


def test_write_records_missing_fields_fall_back_in_source(tmp_path: Path) -> None:
    """Records without ``service_name`` / ``mind_id`` get ``unknown`` in the source tag."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path)
    writer.write_records([{"level": "info", "message": "no context"}], now_iso=_FIXED_TIMESTAMP)
    writer.close()
    lines = _read_lines(path)
    assert lines[0]["source"] == "electron/renderer/service/unknown/unknown"


def test_rotation_prunes_oldest_beyond_retention_cap(tmp_path: Path) -> None:
    """Only the newest ``max_rotated_count`` rotated files survive rotation."""
    path = tmp_path / "events.jsonl"
    writer = IframeLogWriter(file_path=path, max_size_bytes=64, max_rotated_count=2)
    # Force several rotations by writing large messages repeatedly. Each pair
    # is one large write that pushes over the cap, then one short write that
    # triggers the pre-write rotation check.
    for index in range(4):
        writer.write_records(
            [{"level": "info", "message": f"{index}:" + "a" * 80, "service_name": "web", "mind_id": "m"}]
        )
        writer.write_records([{"level": "info", "message": "short", "service_name": "web", "mind_id": "m"}])
    writer.close()
    rotated = _find_rotated_siblings(tmp_path)
    assert len(rotated) == 2, f"expected 2 retained rotations, found {len(rotated)}"
