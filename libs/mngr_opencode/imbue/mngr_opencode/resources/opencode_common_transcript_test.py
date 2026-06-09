"""Tests for the opencode common transcript converter (opencode_common_transcript.sh).

The converter reads the raw transcript at
``$MNGR_AGENT_STATE_DIR/logs/opencode_transcript/events.jsonl`` -- one JSON line
per OpenCode event, as ``{"type": ..., "properties": ...}``, exactly as the
in-process plugin writes it -- and converts the semantically important events
into the common format at
``events/opencode/common_transcript/events.jsonl``. Tests seed that raw file
directly (rather than running OpenCode) and run the converter once via
``--single-pass``.

The raw event shapes mirror the @opencode-ai/sdk 1.16.2 types verified live:
``message.updated -> {info: Message}`` and
``message.part.updated -> {part: Part}`` (text / tool parts).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_SCRIPT_PATH = Path(__file__).parent / "opencode_common_transcript.sh"


def _message_event(
    *,
    message_id: str,
    session_id: str,
    role: str,
    created_ms: int = 1780000000000,
    provider_id: str | None = None,
    model_id: str | None = None,
    finish: str | None = None,
) -> str:
    info: dict[str, Any] = {"id": message_id, "sessionID": session_id, "role": role, "time": {"created": created_ms}}
    if provider_id is not None:
        info["providerID"] = provider_id
    if model_id is not None:
        info["modelID"] = model_id
    if finish is not None:
        info["finish"] = finish
    return json.dumps({"type": "message.updated", "properties": {"info": info}})


def _text_part_event(*, part_id: str, message_id: str, session_id: str, text: str) -> str:
    part = {"id": part_id, "messageID": message_id, "sessionID": session_id, "type": "text", "text": text}
    return json.dumps({"type": "message.part.updated", "properties": {"part": part}})


def _tool_part_event(
    *,
    part_id: str,
    message_id: str,
    session_id: str,
    call_id: str,
    tool: str,
    status: str,
    tool_input: dict[str, Any] | None = None,
    output: str | None = None,
    error: str | None = None,
) -> str:
    state: dict[str, Any] = {"status": status, "input": tool_input or {}}
    if output is not None:
        state["output"] = output
    if error is not None:
        state["error"] = error
    part = {
        "id": part_id,
        "messageID": message_id,
        "sessionID": session_id,
        "type": "tool",
        "callID": call_id,
        "tool": tool,
        "state": state,
    }
    return json.dumps({"type": "message.part.updated", "properties": {"part": part}})


def _setup_state_dir(tmp_path: Path, raw_lines: list[str]) -> Path:
    state_dir = tmp_path / "agent"
    (state_dir / "commands").mkdir(parents=True)
    (state_dir / "commands" / "mngr_log.sh").write_text(
        'log_info() { :; }\nlog_warn() { echo "WARN: $*" >&2; }\nlog_debug() { :; }\n'
    )
    raw_path = state_dir / "logs" / "opencode_transcript" / "events.jsonl"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("\n".join(raw_lines) + "\n")
    return state_dir


def _run_converter(state_dir: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH), "--single-pass"],
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"converter failed: {result.stderr}"
    output_path = state_dir / "events" / "opencode" / "common_transcript" / "events.jsonl"
    if not output_path.exists():
        return []
    return [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()]


def test_converts_user_text_to_user_message(tmp_path: Path) -> None:
    raw = [
        _message_event(message_id="msg_u", session_id="ses_1", role="user"),
        _text_part_event(part_id="prt_u", message_id="msg_u", session_id="ses_1", text="hello world"),
    ]
    events = _run_converter(_setup_state_dir(tmp_path, raw))
    user_events = [e for e in events if e["type"] == "user_message"]
    assert len(user_events) == 1
    assert user_events[0]["content"] == "hello world"
    assert user_events[0]["role"] == "user"
    assert user_events[0]["conversation_id"] == "ses_1"
    assert user_events[0]["event_id"].endswith("-user")


def test_converts_assistant_text_and_completed_tool_to_message_and_result(tmp_path: Path) -> None:
    raw = [
        _message_event(
            message_id="msg_a",
            session_id="ses_1",
            role="assistant",
            provider_id="opencode",
            model_id="deepseek-v4-flash-free",
            finish="stop",
        ),
        _text_part_event(part_id="prt_t", message_id="msg_a", session_id="ses_1", text="Reading the file."),
        _tool_part_event(
            part_id="prt_tool",
            message_id="msg_a",
            session_id="ses_1",
            call_id="call_1",
            tool="read",
            status="completed",
            tool_input={"filePath": "NOTES.md"},
            output="file contents here",
        ),
    ]
    events = _run_converter(_setup_state_dir(tmp_path, raw))
    assistant = [e for e in events if e["type"] == "assistant_message"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(assistant) == 1
    assert assistant[0]["text"] == "Reading the file."
    assert assistant[0]["model"] == "opencode/deepseek-v4-flash-free"
    assert assistant[0]["stop_reason"] == "stop"
    assert len(assistant[0]["tool_calls"]) == 1
    assert assistant[0]["tool_calls"][0]["tool_name"] == "read"
    assert assistant[0]["tool_calls"][0]["input_preview"] != ""
    assert len(results) == 1
    assert results[0]["tool_name"] == "read"
    assert "file contents here" in results[0]["output"]
    assert results[0]["is_error"] is False
    # The tool_result follows its assistant_message in file order.
    assert events.index(assistant[0]) < events.index(results[0])


def test_errored_tool_part_marks_result_as_error(tmp_path: Path) -> None:
    raw = [
        _message_event(message_id="msg_a", session_id="ses_1", role="assistant"),
        _tool_part_event(
            part_id="prt_e",
            message_id="msg_a",
            session_id="ses_1",
            call_id="call_e",
            tool="bash",
            status="error",
            error="command failed: boom",
        ),
    ]
    events = _run_converter(_setup_state_dir(tmp_path, raw))
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 1
    assert results[0]["is_error"] is True
    assert "boom" in results[0]["output"]


def test_pending_tool_part_does_not_emit_result(tmp_path: Path) -> None:
    raw = [
        _message_event(message_id="msg_a", session_id="ses_1", role="assistant"),
        _tool_part_event(
            part_id="prt_p",
            message_id="msg_a",
            session_id="ses_1",
            call_id="call_p",
            tool="read",
            status="pending",
        ),
    ]
    events = _run_converter(_setup_state_dir(tmp_path, raw))
    assert [e for e in events if e["type"] == "tool_result"] == []


def test_streaming_part_resolves_to_final_text_via_full_rewrite(tmp_path: Path) -> None:
    """A later (fuller) snapshot of the same part id wins -- no stale partial frozen in."""
    state_dir = _setup_state_dir(
        tmp_path,
        [
            _message_event(message_id="msg_a", session_id="ses_1", role="assistant"),
            _text_part_event(part_id="prt_t", message_id="msg_a", session_id="ses_1", text="Hel"),
            _text_part_event(part_id="prt_t", message_id="msg_a", session_id="ses_1", text="Hello, done."),
        ],
    )
    events = _run_converter(state_dir)
    assistant = [e for e in events if e["type"] == "assistant_message"]
    assert len(assistant) == 1
    assert assistant[0]["text"] == "Hello, done."


def test_idempotent_across_runs(tmp_path: Path) -> None:
    state_dir = _setup_state_dir(
        tmp_path,
        [
            _message_event(message_id="msg_u", session_id="ses_1", role="user"),
            _text_part_event(part_id="prt_u", message_id="msg_u", session_id="ses_1", text="hi"),
        ],
    )
    first = _run_converter(state_dir)
    second = _run_converter(state_dir)
    assert first == second


def test_malformed_raw_lines_are_skipped(tmp_path: Path) -> None:
    raw = [
        "this is not json",
        _message_event(message_id="msg_u", session_id="ses_1", role="user"),
        _text_part_event(part_id="prt_u", message_id="msg_u", session_id="ses_1", text="survived"),
    ]
    events = _run_converter(_setup_state_dir(tmp_path, raw))
    assert [e for e in events if e["type"] == "user_message"][0]["content"] == "survived"
