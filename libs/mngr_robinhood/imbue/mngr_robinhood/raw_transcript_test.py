import json

from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr_robinhood.raw_transcript import RawTranscriptParser


def _make_parser() -> RawTranscriptParser:
    return RawTranscriptParser(warner=MalformedJsonLineWarner(source_description="test"))


def _assistant_raw(uuid: str, text: str, tool_uses: list[dict[str, object]] | None = None) -> str:
    content: list[dict[str, object]] = []
    if text:
        content.append({"type": "text", "text": text})
    if tool_uses:
        content.extend(tool_uses)
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "model": "claude-test",
                "content": content,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )


def _user_raw(uuid: str, content: object, is_meta: bool = False) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": "2026-01-01T00:00:01Z",
            "isMeta": is_meta,
            "message": {"role": "user", "content": content},
        }
    )


def test_assistant_text_becomes_assistant_message_event() -> None:
    parser = _make_parser()
    events = parser.parse_lines([_assistant_raw("u1", "hello world")])
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "assistant_message"
    assert event["text"] == "hello world"
    assert event["model"] == "claude-test"
    assert event["stop_reason"] == "end_turn"
    assert event["message_uuid"] == "u1"


def test_assistant_with_multiple_text_blocks_joins_with_newlines() -> None:
    parser = _make_parser()
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "u-multi",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ]
            },
        }
    )
    events = parser.parse_lines([raw])
    assert events[0]["text"] == "first\nsecond"


def test_assistant_tool_use_populates_tool_name_map() -> None:
    parser = _make_parser()
    parser.parse_lines(
        [
            _assistant_raw(
                "u2",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"cmd": "ls"}}],
            )
        ]
    )
    assert parser.tool_name_by_call_id["call-1"] == "Bash"


def test_assistant_tool_use_emits_tool_call_with_input_preview() -> None:
    parser = _make_parser()
    events = parser.parse_lines(
        [
            _assistant_raw(
                "u2b",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"cmd": "ls"}}],
            )
        ]
    )
    assert len(events) == 1
    assert events[0]["tool_calls"] == [
        {"tool_call_id": "call-1", "tool_name": "Bash", "input_preview": '{"cmd":"ls"}'},
    ]


def test_assistant_tool_use_input_preview_is_truncated() -> None:
    # A long tool input must be truncated to _MAX_INPUT_PREVIEW_LENGTH (200)
    # with a "..." suffix so synthesized lines stay bounded.
    long_value = "x" * 500
    parser = _make_parser()
    events = parser.parse_lines(
        [
            _assistant_raw(
                "u2c",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"cmd": long_value}}],
            )
        ]
    )
    preview = events[0]["tool_calls"][0]["input_preview"]
    assert preview.endswith("...")
    assert len(preview) == 200 + len("...")


def test_assistant_usage_is_converted() -> None:
    # _assistant_raw sets usage {"input_tokens": 1, "output_tokens": 2}; the
    # converted event must carry the full common-transcript usage shape, with
    # the absent cache fields explicitly None (not dropped).
    parser = _make_parser()
    events = parser.parse_lines([_assistant_raw("u-usage", "hi")])
    assert events[0]["usage"] == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }


def test_assistant_empty_usage_becomes_none() -> None:
    parser = _make_parser()
    raw = json.dumps(
        {
            "type": "assistant",
            "uuid": "u-no-usage",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [{"type": "text", "text": "hi"}], "usage": {}},
        }
    )
    events = parser.parse_lines([raw])
    assert events[0]["usage"] is None


def test_user_text_becomes_user_message_event() -> None:
    parser = _make_parser()
    events = parser.parse_lines([_user_raw("u3", "hi from user")])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "hi from user"


def test_user_meta_event_becomes_tool_result_event() -> None:
    parser = _make_parser()
    events = parser.parse_lines([_user_raw("u4", "stop hook fired", is_meta=True)])
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "meta"


def test_tool_result_uses_tool_name_from_prior_assistant_tool_use() -> None:
    parser = _make_parser()
    parser.parse_lines(
        [
            _assistant_raw(
                "u5",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-X", "name": "Read", "input": {"path": "x"}}],
            )
        ]
    )
    user_with_result = json.dumps(
        {
            "type": "user",
            "uuid": "u6",
            "timestamp": "2026-01-01T00:00:02Z",
            "isMeta": False,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-X", "content": "file contents"}],
            },
        }
    )
    events = parser.parse_lines([user_with_result])
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) == 1
    assert tool_result_events[0]["tool_name"] == "Read"
    assert tool_result_events[0]["output"] == "file contents"
    assert tool_result_events[0]["tool_call_id"] == "call-X"


def test_tool_result_list_content_is_flattened() -> None:
    # A tool_result whose ``content`` is a list of text blocks / bare strings
    # (rather than a plain string) must flatten to newline-joined text.
    parser = _make_parser()
    parser.parse_lines(
        [
            _assistant_raw(
                "u-list",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-L", "name": "Read", "input": {}}],
            )
        ]
    )
    user_with_list_result = _user_raw(
        "u-list-r",
        [
            {
                "type": "tool_result",
                "tool_use_id": "call-L",
                "content": [
                    {"type": "text", "text": "line one"},
                    "line two",
                    {"type": "image", "data": "ignored"},
                ],
            }
        ],
    )
    events = parser.parse_lines([user_with_list_result])
    tool_result_events = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_result_events) == 1
    assert tool_result_events[0]["output"] == "line one\nline two"


def test_tool_result_output_is_truncated() -> None:
    # A tool_result output longer than _MAX_OUTPUT_LENGTH (2000) is truncated
    # with a "..." suffix.
    parser = _make_parser()
    parser.parse_lines(
        [
            _assistant_raw(
                "u-trunc",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-T", "name": "Bash", "input": {}}],
            )
        ]
    )
    long_output = "y" * 5000
    user_with_long_result = _user_raw(
        "u-trunc-r",
        [{"type": "tool_result", "tool_use_id": "call-T", "content": long_output}],
    )
    events = parser.parse_lines([user_with_long_result])
    output = [e for e in events if e["type"] == "tool_result"][0]["output"]
    assert output.endswith("...")
    assert len(output) == 2000 + len("...")


def test_user_message_with_text_and_tool_result_emits_both_events() -> None:
    # A user message containing BOTH a text block and a tool_result block (not
    # the text-only or tool_result-only cases the other tests cover) must emit a
    # user_message event AND a tool_result event.
    parser = _make_parser()
    parser.parse_lines(
        [
            _assistant_raw(
                "u-mix",
                "",
                tool_uses=[{"type": "tool_use", "id": "call-M", "name": "Read", "input": {}}],
            )
        ]
    )
    mixed = _user_raw(
        "u-mix-r",
        [
            {"type": "text", "text": "here is some context"},
            {"type": "tool_result", "tool_use_id": "call-M", "content": "file body"},
        ],
    )
    events = parser.parse_lines([mixed])
    by_type = {e["type"]: e for e in events}
    assert set(by_type) == {"user_message", "tool_result"}
    assert by_type["user_message"]["content"] == "here is some context"
    assert by_type["tool_result"]["output"] == "file body"
    assert by_type["tool_result"]["tool_name"] == "Read"


def test_unknown_event_types_are_dropped() -> None:
    parser = _make_parser()
    events = parser.parse_lines(
        [
            json.dumps({"type": "summary", "uuid": "u7", "timestamp": "2026-01-01T00:00:00Z", "summary": "ignored"}),
            json.dumps({"type": "attachment", "uuid": "u8", "timestamp": "2026-01-01T00:00:00Z"}),
            json.dumps({"type": "last-prompt", "leafUuid": "x"}),
        ]
    )
    assert events == []


def test_blank_and_malformed_lines_are_skipped() -> None:
    parser = _make_parser()
    events = parser.parse_lines(["", "   ", "not-json"])
    assert events == []


def test_event_missing_uuid_is_skipped() -> None:
    parser = _make_parser()
    bad = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
    events = parser.parse_lines([bad])
    assert events == []
