import io
import json

from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.data_types import ResultMeta
from imbue.mngr_robinhood.output_modes import StreamingOutputWriter
from imbue.mngr_robinhood.output_modes import _parse_input_preview
from imbue.mngr_robinhood.output_modes import build_result_envelope
from imbue.mngr_robinhood.output_modes import build_system_init_envelope
from imbue.mngr_robinhood.output_modes import transcript_event_to_stream_json


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


def _tool_result_event(name: str, tool_call_id: str, output: str, is_error: bool) -> dict[str, object]:
    return {
        "type": "tool_result",
        "event_id": f"evt-{name}",
        "tool_call_id": tool_call_id,
        "output": output,
        "is_error": is_error,
        "message_uuid": f"uuid-{name}",
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


def test_build_result_envelope_error_falls_back_when_text_missing() -> None:
    meta = ResultMeta(session_id="session-1", duration_ms=2, is_error=True, error_text=None)
    envelope = build_result_envelope(text="ignored", meta=meta, turn_count=1)
    assert envelope["subtype"] == "error"
    assert envelope["is_error"] is True
    # claude -p's native envelope always carries a string here; verify we
    # never emit JSON null even when error_text is missing.
    assert isinstance(envelope["result"], str)
    assert envelope["result"] != ""


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
    # The inner message is dumped from the anthropic Python SDK's Message, so the text block also
    # carries that model's optional `citations` field (null here). This is a known, documented
    # departure from the real `claude` binary, which omits `citations` entirely -- see the
    # `imbue.mngr_claude.stream_json` module docstring.
    assert message["content"] == [{"type": "text", "text": "hi", "citations": None}]


def test_transcript_user_event_to_stream_json() -> None:
    converted = transcript_event_to_stream_json(_user_event("hi"), "sess-1")
    assert converted is not None
    assert converted["type"] == "user"
    # Assert the full converted message, not just the envelope type: a bug that
    # read the wrong key or mangled the content (e.g. emitted a list) would
    # leave the type as "user" and slip past a type-only assertion.
    assert converted["message"] == {"role": "user", "content": "hi"}
    assert converted["session_id"] == "sess-1"


def test_transcript_assistant_event_with_tool_use_emits_tool_use_block() -> None:
    event = _assistant_event("look")
    event["tool_calls"] = [
        {"tool_call_id": "call-1", "tool_name": "Bash", "input_preview": '{"cmd":"ls"}'},
    ]
    converted = transcript_event_to_stream_json(event, "sess-1")
    assert converted is not None
    content = converted["message"]["content"]
    # The text block comes first, then one tool_use block per tool call with the input_preview
    # parsed back into structured JSON. Both blocks are dumped from the anthropic Python SDK's
    # Message, so they also carry that model's optional null-here fields (`citations` on text,
    # `caller` on tool_use). These are known, documented departures from the real `claude` binary
    # (which omits `citations` and emits a populated `caller`) -- see the
    # `imbue.mngr_claude.stream_json` module docstring.
    assert content == [
        {"type": "text", "text": "look", "citations": None},
        {"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"cmd": "ls"}, "caller": None},
    ]


def test_transcript_tool_result_event_to_stream_json() -> None:
    event = _tool_result_event("tr", tool_call_id="call-1", output="command output", is_error=True)
    converted = transcript_event_to_stream_json(event, "sess-1")
    assert converted is not None
    assert converted["type"] == "user"
    assert converted["session_id"] == "sess-1"
    assert converted["message"]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call-1",
        "content": "command output",
        "is_error": True,
    }


def test_transcript_tool_result_event_is_error_false() -> None:
    # ``is_error`` is coerced via ``bool(...)``; verify the False case explicitly
    # so a regression that always emitted True (or dropped the key) is caught.
    event = _tool_result_event("tr2", tool_call_id="call-2", output="ok", is_error=False)
    converted = transcript_event_to_stream_json(event, "sess-1")
    assert converted is not None
    assert converted["message"]["content"][0]["is_error"] is False


def test_parse_input_preview_empty_returns_empty_dict() -> None:
    assert _parse_input_preview("") == {}


def test_parse_input_preview_valid_json_returns_parsed_object() -> None:
    assert _parse_input_preview('{"path":"x","n":3}') == {"path": "x", "n": 3}


def test_parse_input_preview_unparseable_returns_raw_string() -> None:
    # A truncated/invalid preview is surfaced verbatim (best-effort) rather than
    # raising, so the consumer still sees something.
    truncated = '{"path":"long val'
    assert _parse_input_preview(truncated) == truncated


def test_transcript_unknown_event_dropped() -> None:
    converted = transcript_event_to_stream_json({"type": "something_else"}, "sess-1")
    assert converted is None


def test_text_writer_concatenates_assistant_turns() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello"), _user_event("ignored"), _assistant_event("world")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    assert stdout.getvalue() == "helloworld\n"


def test_text_writer_dedupes_events_by_id() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello"), _assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    assert stdout.getvalue() == "hello\n"


def test_json_writer_emits_single_envelope() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.JSON, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["type"] == "result"
    assert parsed["result"] == "hello"
    assert parsed["session_id"] == "session-1"


def test_json_writer_reports_orchestrator_turn_count() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.JSON, session_id="session-1", stdout=stdout)
    # Two assistant_message events arrive within a single conversational turn.
    writer.emit_events([_assistant_event("hello"), _assistant_event("world")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    parsed = json.loads(stdout.getvalue().strip())
    # The field name comes from claude -p's native wire shape; we deliberately
    # use the literal key to assert wire-compatibility.
    assert parsed["num_turns"] == 1


def test_stream_json_writer_emits_init_first_and_result_last() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.STREAM_JSON, session_id="session-1", stdout=stdout)
    writer.emit_events([_assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 3
    assert lines[0]["type"] == "system"
    assert lines[0]["subtype"] == "init"
    assert lines[1]["type"] == "assistant"
    assert lines[2]["type"] == "result"


def test_stream_json_writer_suppresses_user_messages_by_default() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.STREAM_JSON, session_id="session-1", stdout=stdout)
    writer.emit_events([_user_event("hi"), _assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    types = [json.loads(line)["type"] for line in stdout.getvalue().splitlines()]
    # Default matches claude -p: user prompts are not echoed back into the stream.
    assert types == ["system", "assistant", "result"]


def test_writer_tracks_assistant_message_count_across_events() -> None:
    """Every assistant_message event bumps the count, even ones with empty text
    (which are common for tool-only cycles within a single turn). Non-assistant
    events (user_message, tool_result) do not advance the counter."""
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="s", stdout=stdout)
    assert writer.assistant_message_count == 0

    writer.emit_events([_assistant_event("hi")])
    assert writer.assistant_message_count == 1

    empty_text_assistant: dict[str, object] = {
        "type": "assistant_message",
        "event_id": "evt-tool-only",
        "text": "",
        "tool_calls": [],
        "model": "claude-test",
        "stop_reason": "tool_use",
        "usage": None,
        "message_uuid": "uuid-tool-only",
    }
    writer.emit_events([empty_text_assistant])
    assert writer.assistant_message_count == 2

    writer.emit_events([_user_event("ignored")])
    assert writer.assistant_message_count == 2


def test_writer_tracks_last_assistant_stop_reason() -> None:
    """The orchestrator uses ``last_assistant_stop_reason`` to gate end-of-turn
    finalization, so it must always reflect the most recently observed value."""
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="s", stdout=stdout)
    assert writer.last_assistant_stop_reason is None

    tool_use_assistant: dict[str, object] = {
        "type": "assistant_message",
        "event_id": "evt-1",
        "text": "calling a tool",
        "tool_calls": [],
        "model": "claude-test",
        "stop_reason": "tool_use",
        "usage": None,
        "message_uuid": "uuid-1",
    }
    writer.emit_events([tool_use_assistant])
    assert writer.last_assistant_stop_reason == "tool_use"

    end_turn_assistant: dict[str, object] = {
        "type": "assistant_message",
        "event_id": "evt-2",
        "text": "final answer",
        "tool_calls": [],
        "model": "claude-test",
        "stop_reason": "end_turn",
        "usage": None,
        "message_uuid": "uuid-2",
    }
    writer.emit_events([end_turn_assistant])
    assert writer.last_assistant_stop_reason == "end_turn"


def test_writer_ignores_non_string_stop_reason() -> None:
    """A missing or non-string ``stop_reason`` must not clobber the last seen
    value with ``None`` -- otherwise a malformed mid-stream event would erase
    a previously observed terminal stop_reason and trigger an unnecessary
    quiesce wait downstream."""
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="s", stdout=stdout)
    writer.emit_events([_assistant_event("done")])
    assert writer.last_assistant_stop_reason == "end_turn"

    no_stop_reason: dict[str, object] = {
        "type": "assistant_message",
        "event_id": "evt-mal",
        "text": "more",
        "tool_calls": [],
        "model": "claude-test",
        "stop_reason": None,
        "usage": None,
        "message_uuid": "uuid-mal",
    }
    writer.emit_events([no_stop_reason])
    assert writer.last_assistant_stop_reason == "end_turn"


def test_stream_json_writer_replays_user_messages_when_enabled() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.STREAM_JSON,
        session_id="session-1",
        stdout=stdout,
        replay_user_messages=True,
    )
    writer.emit_events([_user_event("hi"), _assistant_event("hello")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    types = [json.loads(line)["type"] for line in stdout.getvalue().splitlines()]
    assert types == ["system", "user", "assistant", "result"]


def test_emit_partial_text_stream_json_writes_text_delta_after_init() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.STREAM_JSON,
        session_id="session-1",
        stdout=stdout,
    )
    writer.emit_partial_text("Hello")
    writer.emit_partial_text(", world")
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    # The system/init envelope is emitted before the first delta.
    assert lines[0]["type"] == "system"
    assert lines[1] == {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        "session_id": "session-1",
    }
    assert lines[2]["event"]["delta"]["text"] == ", world"
    assert writer.has_streamed_partials


def test_emit_partial_text_plain_text_writes_raw_when_enabled() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.TEXT,
        session_id="session-1",
        stdout=stdout,
        stream_plain_text=True,
    )
    writer.emit_partial_text("Hello")
    writer.emit_partial_text(" there")
    assert stdout.getvalue() == "Hello there"


def test_emit_partial_text_text_mode_noop_without_flag() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.TEXT,
        session_id="session-1",
        stdout=stdout,
        stream_plain_text=False,
    )
    writer.emit_partial_text("Hello")
    assert stdout.getvalue() == ""
    assert not writer.has_streamed_partials


def test_finalize_text_suppresses_dump_after_streaming() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.TEXT,
        session_id="session-1",
        stdout=stdout,
        stream_plain_text=True,
    )
    writer.emit_partial_text("streamed body")
    writer.emit_events([_assistant_event("streamed body")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    # The streamed text appears once, followed only by a trailing newline.
    assert stdout.getvalue() == "streamed body\n"


def test_finalize_text_dumps_normally_without_streaming() -> None:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.TEXT,
        session_id="session-1",
        stdout=stdout,
        stream_plain_text=False,
    )
    writer.emit_events([_assistant_event("the answer")])
    writer.finalize(ResultMeta(session_id="session-1", duration_ms=10, is_error=False, error_text=None), turn_count=1)
    assert stdout.getvalue() == "the answer\n"
