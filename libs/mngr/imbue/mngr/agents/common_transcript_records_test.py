"""Unit tests for the canonical common-transcript envelope schema.

The golden records below mirror the *real* output shapes of all five emitters
(claude, antigravity, opencode, pi-coding, codex), captured from their resource
scripts. They are the executable statement of "these five independently written
emitters agree on the shared contract" -- if a schema change rejected any of them,
that would be a regression in the contract, not the emitter.
"""

from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import AssistantMessageRecord
from imbue.mngr.agents.common_transcript_records import ToolResultRecord
from imbue.mngr.agents.common_transcript_records import UserMessageRecord
from imbue.mngr.agents.common_transcript_records import parse_common_transcript_record
from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record

# One representative record per (emitter, type), reflecting each emitter's real
# field set -- including the optional fields that legitimately differ between them
# (usage populated vs null, model "" vs str vs null, conversation_id/message_id
# present only on some). The schema must accept every one.
_VALID_RECORDS: dict[str, dict[str, Any]] = {
    # claude: full assistant payload (model, finish_reason, populated usage, ordered parts).
    "claude_assistant": {
        "timestamp": "2026-06-09T12:00:00Z",
        "type": "assistant_message",
        "event_id": "claude-1",
        "source": "claude/common_transcript",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "text": "hello",
        "tool_calls": [{"tool_call_id": "t1", "tool_name": "bash", "input_preview": "echo hi"}],
        "parts": [
            {"type": "text", "content": "hello"},
            {"type": "tool_call", "tool_call_id": "t1", "tool_name": "bash", "input_preview": "echo hi"},
        ],
        "parts_ordered": True,
        "finish_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
    "claude_tool_result": {
        "timestamp": "2026-06-09T12:00:01Z",
        "type": "tool_result",
        "event_id": "claude-2",
        "source": "claude/common_transcript",
        "tool_call_id": "t1",
        "tool_name": "bash",
        "output": "hi",
        "is_error": False,
    },
    # antigravity: model/finish_reason/usage all null, best-effort parts order (parts_ordered False),
    # plus its conversation_id annotation.
    "antigravity_user": {
        "timestamp": "2026-06-09T12:00:00Z",
        "type": "user_message",
        "event_id": "agy-1",
        "source": "antigravity/common_transcript",
        "role": "user",
        "content": "do the thing",
        "conversation_id": "conv-abc",
    },
    "antigravity_assistant": {
        "timestamp": "2026-06-09T12:00:01Z",
        "type": "assistant_message",
        "event_id": "agy-2",
        "source": "antigravity/common_transcript",
        "role": "assistant",
        "model": None,
        "text": "ok",
        "tool_calls": [],
        "parts": [{"type": "text", "content": "ok"}],
        "parts_ordered": False,
        "finish_reason": None,
        "usage": None,
        "conversation_id": "conv-abc",
    },
    # opencode: model possibly null, usage null, both conversation_id and message_id.
    "opencode_user": {
        "timestamp": "2026-06-09T12:00:00Z",
        "type": "user_message",
        "event_id": "msg1-user",
        "source": "opencode/common_transcript",
        "role": "user",
        "content": "Count slowly",
        "message_id": "msg1",
    },
    "opencode_assistant": {
        "timestamp": "2026-06-09T12:00:01Z",
        "type": "assistant_message",
        "event_id": "msg2-assistant",
        "source": "opencode/common_transcript",
        "role": "assistant",
        "model": "opencode/deepseek-v4-flash-free",
        "text": "1\n2\n3",
        "tool_calls": [],
        "parts": [{"type": "text", "content": "1\n2\n3"}],
        "parts_ordered": True,
        "finish_reason": None,
        "usage": None,
        "conversation_id": "ses_1",
        "message_id": "msg2",
    },
    "opencode_tool_result": {
        "timestamp": "2026-06-09T12:00:02Z",
        "type": "tool_result",
        "event_id": "prt1-tool_result",
        "source": "opencode/common_transcript",
        "tool_call_id": "call_1",
        "tool_name": "bash",
        "output": "SEEDED",
        "is_error": False,
        "message_id": "msg2",
    },
    # pi-coding: populated usage, model as a plain string, ordered parts, NO finish_reason field at all.
    "pi_user": {
        "timestamp": "2026-06-09T12:00:00Z",
        "type": "user_message",
        "event_id": "pi-0",
        "source": "pi-coding/common_transcript",
        "role": "user",
        "content": "the secret is 1234",
    },
    "pi_assistant": {
        "timestamp": "2026-06-09T12:00:01Z",
        "type": "assistant_message",
        "event_id": "pi-1",
        "source": "pi-coding/common_transcript",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "text": "ACK",
        "tool_calls": [{"tool_call_id": "c1", "tool_name": "bash", "input_preview": "echo SEEDED"}],
        "parts": [
            {"type": "text", "content": "ACK"},
            {"type": "tool_call", "tool_call_id": "c1", "tool_name": "bash", "input_preview": "echo SEEDED"},
        ],
        "parts_ordered": True,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    },
    "pi_tool_result": {
        "timestamp": "2026-06-09T12:00:02Z",
        "type": "tool_result",
        "event_id": "pi-2",
        "source": "pi-coding/common_transcript",
        "tool_call_id": "c1",
        "tool_name": "bash",
        "output": "SEEDED",
        "is_error": False,
    },
    # codex: text-only assistant messages -- model/finish_reason/usage all null, parts is just
    # the text (parts_ordered True, trivially), no conversation_id/message_id.
    "codex_user": {
        "timestamp": "2026-06-09T12:00:00Z",
        "type": "user_message",
        "event_id": "codex-1",
        "source": "codex/common_transcript",
        "role": "user",
        "content": "do the thing",
    },
    "codex_assistant": {
        "timestamp": "2026-06-09T12:00:01Z",
        "type": "assistant_message",
        "event_id": "codex-2",
        "source": "codex/common_transcript",
        "role": "assistant",
        "model": None,
        "text": "done",
        "tool_calls": [],
        "parts": [{"type": "text", "content": "done"}],
        "parts_ordered": True,
        "finish_reason": None,
        "usage": None,
    },
    "codex_tool_result": {
        "timestamp": "2026-06-09T12:00:02Z",
        "type": "tool_result",
        "event_id": "codex-3",
        "source": "codex/common_transcript",
        "tool_call_id": "tc1",
        "tool_name": "shell",
        "output": "ok",
        "is_error": False,
    },
}


@pytest.mark.parametrize("name", sorted(_VALID_RECORDS))
def test_real_emitter_record_shapes_validate(name: str) -> None:
    record = _VALID_RECORDS[name]
    assert validate_common_transcript_record(record) is None, f"{name} should conform"
    # parse returns a typed record of the right class.
    parsed = parse_common_transcript_record(record)
    assert parsed.type == record["type"]


def test_parsed_types_match_expected_classes() -> None:
    assert isinstance(parse_common_transcript_record(_VALID_RECORDS["pi_user"]), UserMessageRecord)
    assert isinstance(parse_common_transcript_record(_VALID_RECORDS["pi_assistant"]), AssistantMessageRecord)
    assert isinstance(parse_common_transcript_record(_VALID_RECORDS["pi_tool_result"]), ToolResultRecord)


def test_assistant_optional_fields_default_when_absent() -> None:
    # A record omitting the optional fields must get the defaults rather than fail: pi really
    # omits finish_reason, and a minimal record omits parts/parts_ordered too.
    bare = {
        "type": "assistant_message",
        "timestamp": "t",
        "event_id": "e",
        "source": "s",
        "text": "hi",
    }
    parsed = parse_common_transcript_record(bare)
    assert isinstance(parsed, AssistantMessageRecord)
    assert parsed.finish_reason is None
    assert parsed.parts == ()
    assert parsed.parts_ordered is True


def test_ordered_parts_round_trip_in_order() -> None:
    # Every assistant record carries an ordered parts[] preserving text/tool_call interleaving.
    parsed = parse_common_transcript_record(_VALID_RECORDS["claude_assistant"])
    assert isinstance(parsed, AssistantMessageRecord)
    assert [p.type for p in parsed.parts] == ["text", "tool_call"]
    assert parsed.parts_ordered is True


def test_best_effort_order_is_flagged() -> None:
    # antigravity can only synthesize a best-effort order, marked by parts_ordered=False.
    parsed = parse_common_transcript_record(_VALID_RECORDS["antigravity_assistant"])
    assert isinstance(parsed, AssistantMessageRecord)
    assert parsed.parts_ordered is False


def test_unknown_part_type_is_rejected() -> None:
    record = dict(_VALID_RECORDS["claude_assistant"])
    # A reasoning part is not modelled yet; an unknown part type must be surfaced, not accepted.
    record["parts"] = [{"type": "reasoning", "content": "secret"}]
    error = validate_common_transcript_record(record)
    assert error is not None and "parts" in error


def test_unknown_extra_fields_are_tolerated() -> None:
    record = dict(_VALID_RECORDS["pi_user"])
    record["some_future_annotation"] = "whatever"
    assert validate_common_transcript_record(record) is None


def test_user_message_without_content_is_rejected() -> None:
    bare = {"type": "user_message", "timestamp": "t", "event_id": "e", "source": "s"}
    error = validate_common_transcript_record(bare)
    assert error is not None and "content" in error


def test_missing_envelope_field_is_rejected() -> None:
    record = dict(_VALID_RECORDS["pi_user"])
    del record["event_id"]
    error = validate_common_transcript_record(record)
    assert error is not None and "event_id" in error


def test_wrong_type_for_required_field_is_rejected() -> None:
    record = dict(_VALID_RECORDS["pi_tool_result"])
    # is_error must be a bool, not a string.
    record["is_error"] = "nope"
    error = validate_common_transcript_record(record)
    assert error is not None and "is_error" in error


def test_unknown_record_type_is_rejected() -> None:
    # An unrecognised `type` means the emitter introduced a record type the shared
    # schema does not know -- surface it rather than silently accept.
    error = validate_common_transcript_record({"type": "thinking", "timestamp": "t", "event_id": "e", "source": "s"})
    assert error is not None


def test_tool_call_requires_its_fields() -> None:
    record = dict(_VALID_RECORDS["pi_assistant"])
    # A tool_call missing tool_call_id + input_preview must be rejected.
    record["tool_calls"] = [{"tool_name": "bash"}]
    error = validate_common_transcript_record(record)
    assert error is not None and "tool_calls" in error
