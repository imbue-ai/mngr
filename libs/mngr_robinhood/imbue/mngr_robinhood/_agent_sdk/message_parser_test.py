from typing import Any

from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import SystemMessage
from claude_agent_sdk import TextBlock
from claude_agent_sdk import ThinkingBlock
from claude_agent_sdk import ToolResultBlock
from claude_agent_sdk import ToolUseBlock
from claude_agent_sdk import UserMessage

from imbue.mngr_robinhood._agent_sdk.message_parser import build_result_message
from imbue.mngr_robinhood._agent_sdk.message_parser import build_system_init_message
from imbue.mngr_robinhood._agent_sdk.message_parser import collect_assistant_text
from imbue.mngr_robinhood._agent_sdk.message_parser import parse_content_blocks
from imbue.mngr_robinhood._agent_sdk.message_parser import parse_transcript_event
from imbue.mngr_robinhood._agent_sdk.message_parser import parse_transcript_events


def _assistant_event(uuid: str, content: list[dict[str, Any]], **message_overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "model": "claude-haiku-4-5",
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 7},
    }
    message.update(message_overrides)
    return {"type": "assistant", "uuid": uuid, "sessionId": "sess-1", "message": message}


def _user_event(uuid: str, content: Any, is_meta: bool = False) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "sess-1",
        "isMeta": is_meta,
        "message": {"role": "user", "content": content},
    }


def test_assistant_text_block_becomes_text_block() -> None:
    message = parse_transcript_event(_assistant_event("a1", [{"type": "text", "text": "hello"}]))
    assert isinstance(message, AssistantMessage)
    assert message.model == "claude-haiku-4-5"
    assert message.stop_reason == "end_turn"
    assert message.session_id == "sess-1"
    assert message.uuid == "a1"
    assert message.usage == {"input_tokens": 3, "output_tokens": 7}
    assert len(message.content) == 1
    block = message.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "hello"


def test_assistant_message_id_is_read_from_message_id_field() -> None:
    message = parse_transcript_event(_assistant_event("a1", [{"type": "text", "text": "hi"}], id="msg_0123"))
    assert isinstance(message, AssistantMessage)
    assert message.message_id == "msg_0123"


def test_assistant_parent_tool_use_id_defaults_to_none() -> None:
    message = parse_transcript_event(_assistant_event("a1", [{"type": "text", "text": "hi"}]))
    assert isinstance(message, AssistantMessage)
    assert message.parent_tool_use_id is None


def test_thinking_block_is_parsed_with_signature() -> None:
    blocks = parse_content_blocks([{"type": "thinking", "thinking": "pondering", "signature": "sig-9"}])
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, ThinkingBlock)
    assert block.thinking == "pondering"
    assert block.signature == "sig-9"


def test_thinking_block_missing_signature_defaults_to_empty() -> None:
    blocks = parse_content_blocks([{"type": "thinking", "thinking": "pondering"}])
    assert isinstance(blocks[0], ThinkingBlock)
    assert blocks[0].signature == ""


def test_tool_use_block_carries_id_name_input() -> None:
    blocks = parse_content_blocks(
        [{"type": "tool_use", "id": "call-1", "name": "Bash", "input": {"command": "echo hi"}}]
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, ToolUseBlock)
    assert block.id == "call-1"
    assert block.name == "Bash"
    assert block.input == {"command": "echo hi"}


def test_tool_use_block_missing_input_defaults_to_empty_dict() -> None:
    blocks = parse_content_blocks([{"type": "tool_use", "id": "call-1", "name": "Bash"}])
    assert isinstance(blocks[0], ToolUseBlock)
    assert blocks[0].input == {}


def test_assistant_with_mixed_blocks_preserves_order() -> None:
    event = _assistant_event(
        "a1",
        [
            {"type": "text", "text": "running it"},
            {"type": "tool_use", "id": "call-2", "name": "Bash", "input": {"command": "ls"}},
        ],
    )
    message = parse_transcript_event(event)
    assert isinstance(message, AssistantMessage)
    assert isinstance(message.content[0], TextBlock)
    assert isinstance(message.content[1], ToolUseBlock)


def test_user_string_content_stays_a_string() -> None:
    message = parse_transcript_event(_user_event("u1", "what is 2+2?"))
    assert isinstance(message, UserMessage)
    assert message.content == "what is 2+2?"
    assert message.uuid == "u1"


def test_user_tool_result_block_is_parsed() -> None:
    event = _user_event(
        "u2",
        [{"type": "tool_result", "tool_use_id": "call-2", "content": "hi\n", "is_error": False}],
    )
    message = parse_transcript_event(event)
    assert isinstance(message, UserMessage)
    assert isinstance(message.content, list)
    block = message.content[0]
    assert isinstance(block, ToolResultBlock)
    assert block.tool_use_id == "call-2"
    assert block.content == "hi\n"
    assert block.is_error is False


def test_tool_result_with_block_list_content_is_coerced_to_dicts() -> None:
    blocks = parse_content_blocks(
        [{"type": "tool_result", "tool_use_id": "call-3", "content": [{"type": "text", "text": "out"}]}]
    )
    block = blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.content == [{"type": "text", "text": "out"}]


def test_tool_result_error_flag_true() -> None:
    blocks = parse_content_blocks(
        [{"type": "tool_result", "tool_use_id": "call-4", "content": "boom", "is_error": True}]
    )
    block = blocks[0]
    assert isinstance(block, ToolResultBlock)
    assert block.is_error is True


def test_meta_user_event_is_skipped() -> None:
    assert parse_transcript_event(_user_event("u3", "stop hook output", is_meta=True)) is None


def test_summary_event_is_skipped() -> None:
    assert parse_transcript_event({"type": "summary", "uuid": "s1", "summary": "ignored"}) is None


def test_unknown_block_types_are_dropped() -> None:
    blocks = parse_content_blocks([{"type": "image", "source": {}}, {"type": "text", "text": "kept"}])
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)


def test_malformed_blocks_are_tolerated() -> None:
    blocks = parse_content_blocks(["not a dict", {"type": "text"}, {"type": "text", "text": "ok"}])
    # The bare string and the text block missing its ``text`` field are skipped.
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "ok"


def test_parse_transcript_events_preserves_order_and_filters() -> None:
    events = [
        _user_event("u1", "hi"),
        _assistant_event("a1", [{"type": "text", "text": "hello"}]),
        {"type": "summary", "uuid": "s1", "summary": "x"},
        _user_event("u2", "again", is_meta=True),
    ]
    messages = parse_transcript_events(events)
    assert len(messages) == 2
    assert isinstance(messages[0], UserMessage)
    assert isinstance(messages[1], AssistantMessage)


def test_collect_assistant_text_joins_only_assistant_text_blocks() -> None:
    messages = parse_transcript_events(
        [
            _user_event("u1", "ignored user text"),
            _assistant_event("a1", [{"type": "text", "text": "first"}]),
            _assistant_event("a2", [{"type": "text", "text": "second"}]),
        ]
    )
    assert collect_assistant_text(messages) == "first\nsecond"


def test_build_system_init_message_shape() -> None:
    init = build_system_init_message(
        session_id="sess-1", model="claude-haiku-4-5", cwd="/tmp/x", tools=["Bash", "Read"]
    )
    assert isinstance(init, SystemMessage)
    assert init.subtype == "init"
    assert init.data["session_id"] == "sess-1"
    assert init.data["model"] == "claude-haiku-4-5"
    assert init.data["cwd"] == "/tmp/x"
    assert init.data["tools"] == ["Bash", "Read"]


def test_build_result_message_success() -> None:
    result = build_result_message(
        session_id="sess-1",
        is_error=False,
        result_text="done",
        duration_ms=1200,
        duration_api_ms=900,
        turn_count=1,
        usage={"input_tokens": 3, "output_tokens": 7},
        total_cost_usd=0.001,
        model_usage={"claude-haiku-4-5": {"input_tokens": 3}},
        permission_denials=[],
        result_uuid="res-uuid-1",
    )
    assert isinstance(result, ResultMessage)
    assert result.subtype == "success"
    assert result.is_error is False
    assert result.result == "done"
    assert result.session_id == "sess-1"
    assert result.total_cost_usd == 0.001
    assert result.permission_denials == []


def test_build_result_message_error_subtype() -> None:
    result = build_result_message(
        session_id="sess-1",
        is_error=True,
        result_text="boom",
        duration_ms=10,
        duration_api_ms=0,
        turn_count=1,
        usage=None,
        total_cost_usd=None,
        model_usage=None,
        permission_denials=None,
        result_uuid="res-uuid-2",
    )
    assert result.subtype == "error"
    assert result.is_error is True
