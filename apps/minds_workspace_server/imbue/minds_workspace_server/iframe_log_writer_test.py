"""Unit tests for the iframe-log writer."""

import json
from pathlib import Path
from typing import Any

from imbue.minds_workspace_server.iframe_log_writer import IframeLogWriter

_FIXED_TIMESTAMP = "2026-04-23T12:00:00.000000000Z"


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_write_records_creates_file_and_envelope(tmp_path: Path) -> None:
    """Each record is wrapped in the envelope and appended as JSONL."""
    path = tmp_path / "iframe.jsonl"
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
    path = tmp_path / "iframe.jsonl"
    writer = IframeLogWriter(file_path=path)
    count = writer.write_records([])
    writer.close()
    assert count == 0
    assert not path.exists()


def test_write_records_appends_across_calls(tmp_path: Path) -> None:
    """Successive batches append to the same file."""
    path = tmp_path / "iframe.jsonl"
    writer = IframeLogWriter(file_path=path)
    writer.write_records([{"level": "info", "message": "first", "service_name": "web", "mind_id": "m1"}])
    writer.write_records([{"level": "info", "message": "second", "service_name": "web", "mind_id": "m1"}])
    writer.close()
    messages = [line["message"] for line in _read_lines(path)]
    assert messages == ["first", "second"]


def test_write_records_rotates_at_max_size(tmp_path: Path) -> None:
    """When the file exceeds max_size_bytes, it is renamed to .1 and a new file opens."""
    path = tmp_path / "iframe.jsonl"
    writer = IframeLogWriter(file_path=path, max_size_bytes=64)
    writer.write_records([{"level": "info", "message": "a" * 80, "service_name": "web", "mind_id": "m1"}])
    # At this point size exceeds cap; the next write triggers the rotation.
    writer.write_records([{"level": "info", "message": "short", "service_name": "web", "mind_id": "m1"}])
    writer.close()
    rotated = path.with_name("iframe.jsonl.1")
    assert rotated.exists()
    assert path.exists()
    assert len(_read_lines(rotated)) == 1
    assert len(_read_lines(path)) == 1


def test_write_records_missing_fields_fall_back_in_source(tmp_path: Path) -> None:
    """Records without ``service_name`` / ``mind_id`` get ``unknown`` in the source tag."""
    path = tmp_path / "iframe.jsonl"
    writer = IframeLogWriter(file_path=path)
    writer.write_records([{"level": "info", "message": "no context"}], now_iso=_FIXED_TIMESTAMP)
    writer.close()
    lines = _read_lines(path)
    assert lines[0]["source"] == "electron/renderer/service/unknown/unknown"


def test_rotation_picks_next_free_suffix(tmp_path: Path) -> None:
    """Rotation never overwrites an existing ``.N`` file."""
    path = tmp_path / "iframe.jsonl"
    (path.parent / "iframe.jsonl.1").write_text("old\n")
    writer = IframeLogWriter(file_path=path, max_size_bytes=32)
    writer.write_records([{"level": "info", "message": "a" * 40, "service_name": "s", "mind_id": "m"}])
    writer.write_records([{"level": "info", "message": "tiny", "service_name": "s", "mind_id": "m"}])
    writer.close()
    # .1 was pre-existing and must survive; rotated file goes to .2.
    assert (path.parent / "iframe.jsonl.1").read_text() == "old\n"
    assert (path.parent / "iframe.jsonl.2").exists()
