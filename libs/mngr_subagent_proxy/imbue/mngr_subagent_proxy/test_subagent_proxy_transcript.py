from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Iterator

import pytest
from loguru import logger

from imbue.mngr.primitives import AgentId
from imbue.mngr_subagent_proxy.subagent_wait import _AgentLocation
from imbue.mngr_subagent_proxy.subagent_wait import _TailState
from imbue.mngr_subagent_proxy.subagent_wait import _extract_assistant_text
from imbue.mngr_subagent_proxy.subagent_wait import _is_end_turn_event
from imbue.mngr_subagent_proxy.subagent_wait import _read_new_jsonl_lines
from imbue.mngr_subagent_proxy.subagent_wait import _resolve_destroyed_result
from imbue.mngr_subagent_proxy.subagent_wait import _truncate_result_text


@contextmanager
def _capture_loguru_messages() -> Iterator[list[str]]:
    """Install a loguru sink that appends formatted messages to a list."""
    captured: list[str] = []

    def sink(message: Any) -> None:
        captured.append(message.record["message"])

    handler_id = logger.add(sink, level="TRACE", format="{message}")
    try:
        yield captured
    finally:
        logger.remove(handler_id)


@pytest.mark.release
def test_end_turn_detection_with_pure_text() -> None:
    """_is_end_turn_event accepts pure-text end_turn and rejects tool_use / malformed events."""
    pure_text_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert _is_end_turn_event(pure_text_event) is True

    tool_use_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "calling a tool"},
                {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
            ],
        },
    }
    assert _is_end_turn_event(tool_use_event) is False

    tool_use_stop_reason_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "thinking"}],
        },
    }
    assert _is_end_turn_event(tool_use_stop_reason_event) is False

    missing_message_event = {"type": "assistant"}
    assert _is_end_turn_event(missing_message_event) is False

    non_assistant_event = {"type": "user", "message": {"stop_reason": "end_turn", "content": []}}
    assert _is_end_turn_event(non_assistant_event) is False

    multi_text_event = {
        "type": "assistant",
        "message": {
            "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "thinking", "thinking": "internal state"},
                {"type": "text", "text": "world"},
                "not-a-dict",
            ],
        },
    }
    assert _extract_assistant_text(multi_text_event) == "hello world"


@pytest.mark.release
def test_jsonl_tail_handles_partial_lines(tmp_path: Path) -> None:
    """_read_new_jsonl_lines parses complete lines, buffers partials, logs on malformed, resets on truncation."""
    transcript = tmp_path / "transcript.jsonl"
    state = _TailState(path=transcript, offset=0)

    first_complete = json.dumps({"type": "assistant", "n": 1}) + "\n"
    partial = json.dumps({"type": "assistant", "n": 2})
    transcript.write_bytes((first_complete + partial).encode("utf-8"))

    parsed = _read_new_jsonl_lines(state)
    assert len(parsed) == 1
    assert parsed[0] == {"type": "assistant", "n": 1}
    assert state.pending_buffer == partial

    remainder = "\n" + json.dumps({"type": "assistant", "n": 3}) + "\n"
    with transcript.open("ab") as handle:
        handle.write(remainder.encode("utf-8"))

    parsed = _read_new_jsonl_lines(state)
    assert len(parsed) == 2
    assert parsed[0] == {"type": "assistant", "n": 2}
    assert parsed[1] == {"type": "assistant", "n": 3}
    assert state.pending_buffer == ""

    with _capture_loguru_messages() as captured:
        with transcript.open("ab") as handle:
            handle.write(b"this is not json\n")
            handle.write((json.dumps({"type": "assistant", "n": 4}) + "\n").encode("utf-8"))
        parsed = _read_new_jsonl_lines(state)

    assert len(parsed) == 1
    assert parsed[0] == {"type": "assistant", "n": 4}
    assert any("Malformed JSONL line" in msg for msg in captured)

    short_content = json.dumps({"type": "assistant", "n": 99}) + "\n"
    transcript.write_bytes(short_content.encode("utf-8"))
    parsed = _read_new_jsonl_lines(state)
    assert state.offset == len(short_content)
    assert parsed == [{"type": "assistant", "n": 99}]


@pytest.mark.release
def test_result_truncation() -> None:
    """_truncate_result_text preserves short text, truncates long text, and clips budget safely."""
    short_text = "a" * 100
    assert _truncate_result_text(short_text, max_chars=200) == short_text

    long_text = "a" * 500
    result = _truncate_result_text(long_text, max_chars=200)
    assert len(result) == 200
    assert result.endswith("\n\n[truncated]")
    assert result.startswith("a")

    # When max_chars is smaller than the truncation suffix, the function clips
    # budget to 0 and returns just the suffix. Accepted behavior: we document
    # that the result may exceed max_chars rather than crashing.
    tiny_result = _truncate_result_text(long_text, max_chars=10)
    assert tiny_result == "\n\n[truncated]"
    assert len(tiny_result) == 13


@pytest.mark.release
def test_destroyed_fallback_from_preserved_sessions(tmp_path: Path) -> None:
    """_resolve_destroyed_result returns the last assistant_message text from preserved events."""
    host_dir = tmp_path / "fake_host_dir"
    host_dir.mkdir()
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent_id = AgentId.generate()
    target_name = "reviewer"
    location = _AgentLocation(host_dir=host_dir, agent_id=agent_id, work_dir=work_dir)

    events_dir = (
        host_dir / "plugin" / "mngr_claude" / "preserved_sessions" / f"{target_name}--{agent_id}" / "common_transcript"
    )
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    lines = [
        json.dumps({"type": "assistant_message", "text": "first"}),
        json.dumps({"type": "user_message", "text": "ignored"}),
        json.dumps({"type": "assistant_message", "text": "last answer"}),
        "this is not valid json",
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert _resolve_destroyed_result(target_name, location) == "[mngr agent destroyed before completion] last answer"

    # Missing preserved-events file returns the prefix with an empty last_text.
    missing_agent_id = AgentId.generate()
    missing_location = _AgentLocation(host_dir=host_dir, agent_id=missing_agent_id, work_dir=work_dir)
    assert _resolve_destroyed_result(target_name, missing_location) == "[mngr agent destroyed before completion] "
