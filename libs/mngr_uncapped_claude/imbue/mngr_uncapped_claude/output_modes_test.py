import io
import json

from imbue.mngr_uncapped_claude.data_types import OutputFormat
from imbue.mngr_uncapped_claude.data_types import ResultMeta
from imbue.mngr_uncapped_claude.output_modes import StreamingOutputWriter
from imbue.mngr_uncapped_claude.output_modes import build_result_envelope
from imbue.mngr_uncapped_claude.output_modes import build_system_init_envelope
from imbue.mngr_uncapped_claude.output_modes import transcript_event_to_stream_json


def _assistant_event(text: str) -> dict[str, object]:
    return {
        "type": "assistant_message",
        "event_id": f"evt-{text}",
        "text": text,
        "tool_calls": [],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": None,
        "message_uuid": f"uuid-{text}",
    }


def _user_event(content: str) -> dict[str, object]:
    return {
        "type": "user_message",
        "event_id": f"evt-{content}",
        "content": content,
        "role": "user",
        "message_uuid": f"uuid-{content}",
    }


def test_build_result_envelope_success() -> None:
    meta = ResultMeta(session_id="session-1", duration_ms=1234, is_error=False, error_text=None)
    envelope = build_result_envelope(text="hi there", meta=meta, turn_count=1)
    assert envelope["type"] == "result"
    assert envelope["subtype"] == "success"
    assert envelope["is_error"] is False
    assert envelope["result"] == "hi there"
    assert envelope["session_id"] == "session-1"
    assert envelope["duration_ms"] == 1234
    assert envelope["total_cost_usd"] == 0.0
    assert envelope["usage"] is None


def test_build_result_envelope_error_substitutes_error_text() -> None:
    meta = ResultMeta(session_id="session-1", duration_ms=2, is_error=True, error_text="boom")
    envelope = build_result_envelope(text="ignored", meta=meta, turn_count=1)
    assert envelope["subtype"] == "error"
    assert envelope["is_error"] is True
    assert envelope["result"] == "boom"


def test_build_system_init_envelope_shape() -> None:
    envelope = build_system_init_envelope("session-2")
    assert envelope["type"] == "system"
    assert envelope["subtype"] == "init"
    assert envelope["session_id"] == "session-2"


def test_transcript_assistant_event_to_stream_json() -> None:
    converted = transcript_event_to_stream_json(_assistant_event("hi"), "sess-1")
    assert converted is not None
    assert converted["type"] == "assistant"
    message = converted["message"]
    assert isinstance(message, dict)
    assert message["role"] == "assistant"
    assert message["content"] == [{"type": "text", "text": "hi"}]


def test_transcript_user_event_to_stream_json() -> None:
    converted = transcript_event_to_stream_json(_user_event("hi"), "sess-1")
    assert converted is not None
    assert converted["type"] == "user"


def test_transcript_unknown_event_dropped() -> None:
    converted = transcript_event_to_stream_json({"type": "something_else"}, "sess-1")
    assert converted is None


def test_text_writer_concatenates_assistant_turns() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello"), _user_event("ignored"), _assistant_event("world")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None))
    assert stdout.getvalue() == "helloworld\n"


def test_text_writer_dedupes_events_by_id() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello"), _assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None))
    assert stdout.getvalue() == "hello\n"


def test_json_writer_emits_single_envelope() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.JSON, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None))
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "result"
    assert parsed["result"] == "hello"
    assert parsed["session_id"] == "session-1"


def test_stream_json_writer_emits_init_first_and_result_last() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.STREAM_JSON, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None))
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 3
    assert lines[0]["type"] == "system"
    assert lines[0]["subtype"] == "init"
    assert lines[1]["type"] == "assistant"
    assert lines[2]["type"] == "result"
