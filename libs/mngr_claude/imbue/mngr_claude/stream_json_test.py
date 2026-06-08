import json
from typing import Any

from anthropic.types import RawContentBlockDeltaEvent
from anthropic.types import RawMessageStartEvent

from imbue.mngr_claude.stream_json import build_assistant_message
from imbue.mngr_claude.stream_json import classify_stream_event
from imbue.mngr_claude.stream_json import content_block_start_event
from imbue.mngr_claude.stream_json import content_block_stop_event
from imbue.mngr_claude.stream_json import decode_stream_line
from imbue.mngr_claude.stream_json import extract_assistant_message_id
from imbue.mngr_claude.stream_json import extract_assistant_text
from imbue.mngr_claude.stream_json import extract_message_start_id
from imbue.mngr_claude.stream_json import extract_text_delta
from imbue.mngr_claude.stream_json import message_delta_event
from imbue.mngr_claude.stream_json import message_start_event
from imbue.mngr_claude.stream_json import message_stop_event
from imbue.mngr_claude.stream_json import parse_assistant_message
from imbue.mngr_claude.stream_json import parse_stream_event
from imbue.mngr_claude.stream_json import text_delta_event
from imbue.mngr_claude.stream_json import validate_stream_event
from imbue.mngr_claude.stream_json import wrap_stream_event

# =============================================================================
# Emit side: builders dump to the wire dict
# =============================================================================


def test_text_delta_event_is_byte_identical_to_legacy_shape() -> None:
    # The CLI token stream only ever emits content_block_delta; its dumped shape must match the
    # dict producers hand-rolled before so the wire output is unchanged.
    assert text_delta_event("hello") == {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hello"},
    }


def test_content_block_stop_and_message_stop_are_byte_identical() -> None:
    assert content_block_stop_event() == {"type": "content_block_stop", "index": 0}
    assert message_stop_event() == {"type": "message_stop"}


def test_message_start_event_carries_id_model_and_zeroed_usage() -> None:
    event = message_start_event("msg-1", "claude-sonnet-4-5")
    assert event["type"] == "message_start"
    message = event["message"]
    assert message["id"] == "msg-1"
    assert message["model"] == "claude-sonnet-4-5"
    assert message["content"] == []
    assert message["stop_reason"] is None
    assert message["usage"]["input_tokens"] == 0
    assert message["usage"]["output_tokens"] == 0


def test_content_block_start_event_opens_empty_text_block() -> None:
    event = content_block_start_event()
    assert event["type"] == "content_block_start"
    assert event["index"] == 0
    assert event["content_block"]["type"] == "text"
    assert event["content_block"]["text"] == ""


def test_message_delta_event_stamps_known_stop_reason() -> None:
    event = message_delta_event("end_turn")
    assert event["type"] == "message_delta"
    assert event["delta"]["stop_reason"] == "end_turn"


def test_message_delta_event_degrades_unknown_stop_reason_to_none() -> None:
    # An unrecognized reason must not fail validation; it degrades to null.
    event = message_delta_event("not_a_real_reason")
    assert event["delta"]["stop_reason"] is None


def test_wrap_stream_event_builds_cli_envelope() -> None:
    assert wrap_stream_event({"type": "message_stop"}, "sess-1") == {
        "type": "stream_event",
        "event": {"type": "message_stop"},
        "session_id": "sess-1",
    }


def test_build_assistant_message_concatenates_text_and_defaults_usage() -> None:
    message = build_assistant_message(
        message_id="m1",
        model="unknown",
        content=[{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
        stop_reason="end_turn",
        usage=None,
    )
    assert message["id"] == "m1"
    assert message["role"] == "assistant"
    assert message["stop_reason"] == "end_turn"
    assert [block["text"] for block in message["content"]] == ["hello ", "world"]
    # usage is absent on the transcript event, so a zeroed stub is injected.
    assert message["usage"]["input_tokens"] == 0
    assert message["usage"]["output_tokens"] == 0


def test_build_assistant_message_preserves_real_usage() -> None:
    message = build_assistant_message(
        message_id="m1",
        model="unknown",
        content=[{"type": "text", "text": "x"}],
        stop_reason=None,
        usage={"input_tokens": 5, "output_tokens": 7},
    )
    assert message["usage"]["input_tokens"] == 5
    assert message["usage"]["output_tokens"] == 7


def test_build_assistant_message_coerces_non_object_tool_input() -> None:
    # robinhood's tool-input preview may be a truncated, non-JSON string; anthropic requires an
    # object, so it is coerced to {} rather than failing validation.
    message = build_assistant_message(
        message_id="m1",
        model="unknown",
        content=[{"type": "tool_use", "id": "t", "name": "Bash", "input": "truncated-not-json"}],
        stop_reason=None,
        usage=None,
    )
    tool_block = message["content"][0]
    assert tool_block["type"] == "tool_use"
    assert tool_block["input"] == {}


def test_build_assistant_message_degrades_unknown_stop_reason() -> None:
    message = build_assistant_message(
        message_id="m1", model="unknown", content=[{"type": "text", "text": "x"}], stop_reason="weird", usage=None
    )
    assert message["stop_reason"] is None


# =============================================================================
# Consume side: validate into the union, dispatch, extract
# =============================================================================


def test_validate_stream_event_narrows_to_typed_member() -> None:
    event = validate_stream_event(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}
    )
    assert isinstance(event, RawContentBlockDeltaEvent)


def test_validate_stream_event_returns_none_for_unmodeled_variant() -> None:
    # A type this anthropic package does not model degrades to None (skip), never raises.
    assert validate_stream_event({"type": "brand_new_event", "foo": 1}) is None
    assert validate_stream_event("not even a dict") is None


def test_classify_stream_event_extracts_text_delta() -> None:
    event = validate_stream_event(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}
    )
    assert event is not None
    info = classify_stream_event(event)
    assert info.delta_text == "hi"
    assert info.message_start_id is None


def test_classify_stream_event_extracts_message_start_id() -> None:
    event = validate_stream_event(message_start_event("msg-7", "claude-sonnet-4-5"))
    assert isinstance(event, RawMessageStartEvent)
    info = classify_stream_event(event)
    assert info.message_start_id == "msg-7"
    assert info.delta_text is None


def test_classify_stream_event_yields_nothing_for_framing() -> None:
    for builder_output in (content_block_start_event(), content_block_stop_event(), message_stop_event()):
        event = validate_stream_event(builder_output)
        assert event is not None
        info = classify_stream_event(event)
        assert info.delta_text is None
        assert info.message_start_id is None


def test_classify_stream_event_ignores_non_text_delta() -> None:
    # input_json_delta is a real delta variant that carries no surfaced text.
    event = validate_stream_event(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}}
    )
    assert isinstance(event, RawContentBlockDeltaEvent)
    assert classify_stream_event(event).delta_text is None


def test_decode_stream_line_skips_non_json_and_non_objects() -> None:
    assert decode_stream_line("") is None
    assert decode_stream_line("not json at all") is None
    assert decode_stream_line("[1, 2, 3]") is None
    assert decode_stream_line('{"type": "x"}') == {"type": "x"}


def test_parse_stream_event_unwraps_cli_envelope() -> None:
    line = json.dumps(wrap_stream_event(text_delta_event("hi"), "sess-1"))
    event = parse_stream_event(line)
    assert isinstance(event, RawContentBlockDeltaEvent)


def test_parse_stream_event_returns_none_for_non_stream_event_lines() -> None:
    assert parse_stream_event(json.dumps({"type": "assistant", "message": {}})) is None
    assert parse_stream_event("garbage") is None


# =============================================================================
# Round-trip property: emit and parse agree
# =============================================================================


def test_round_trip_text_delta() -> None:
    line = json.dumps(wrap_stream_event(text_delta_event("round-trips"), "sess-1"))
    event = parse_stream_event(line)
    assert event is not None
    assert classify_stream_event(event).delta_text == "round-trips"


def test_round_trip_message_start_id() -> None:
    line = json.dumps(wrap_stream_event(message_start_event("msg-rt", "claude-sonnet-4-5"), "sess-1"))
    assert extract_message_start_id(line) == "msg-rt"


def test_extract_text_delta_public_wrapper() -> None:
    line = json.dumps(wrap_stream_event(text_delta_event("delta"), "sess-1"))
    assert extract_text_delta(line) == "delta"
    assert extract_text_delta("not a stream line") is None


# =============================================================================
# Assistant summary: typed extraction with lenient fallback
# =============================================================================


def _assistant_line(message: dict[str, Any]) -> str:
    return json.dumps({"type": "assistant", "message": message, "session_id": "sess-1"})


def test_extract_assistant_text_typed_path() -> None:
    message = build_assistant_message(
        message_id="m1",
        model="unknown",
        content=[{"type": "text", "text": "alpha "}, {"type": "text", "text": "beta"}],
        stop_reason="end_turn",
        usage=None,
    )
    assert extract_assistant_text(_assistant_line(message)) == "alpha beta"
    assert extract_assistant_message_id(_assistant_line(message)) == "m1"


def test_extract_assistant_text_falls_back_for_unmodeled_block_type() -> None:
    # A content block this anthropic package does not model would fail whole-message validation;
    # the lenient fallback still surfaces the text blocks so a valid response is not dropped.
    message = {
        "id": "m1",
        "type": "message",
        "role": "assistant",
        "model": "unknown",
        "content": [
            {"type": "text", "text": "keep "},
            {"type": "some_future_block", "payload": 1},
            {"type": "text", "text": "me"},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    assert parse_assistant_message(message) is None
    assert extract_assistant_text(_assistant_line(message)) == "keep me"


def test_extract_assistant_message_id_fallback_when_validation_fails() -> None:
    # Minimal message (missing required fields) fails typed validation; id is still read leniently.
    message = {"id": "m-lenient", "role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert parse_assistant_message(message) is None
    assert extract_assistant_message_id(_assistant_line(message)) == "m-lenient"


def test_extract_assistant_text_none_for_text_free_message() -> None:
    message = build_assistant_message(
        message_id="m1",
        model="unknown",
        content=[{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}],
        stop_reason="tool_use",
        usage=None,
    )
    assert extract_assistant_text(_assistant_line(message)) is None


def test_extract_assistant_text_none_for_non_assistant_line() -> None:
    assert extract_assistant_text(json.dumps({"type": "result", "is_error": False})) is None
    assert extract_assistant_text("garbage") is None


def test_extract_assistant_text_none_when_content_not_list() -> None:
    # Typed validation fails (content is a string, not a block list); the lenient fallback also
    # finds no text and returns None rather than raising.
    line = _assistant_line({"id": "m1", "role": "assistant", "content": "not_a_list"})
    assert parse_assistant_message({"id": "m1", "role": "assistant", "content": "not_a_list"}) is None
    assert extract_assistant_text(line) is None


def test_extract_message_start_id_none_for_malformed_message_start() -> None:
    # A message_start whose inner `message` is structurally invalid fails validation, so no id is
    # surfaced -- the caller simply loses the deltas-vs-summary correlation, text still streams.
    line = json.dumps(wrap_stream_event({"type": "message_start", "message": "not_a_dict"}, "sess-1"))
    assert extract_message_start_id(line) is None
