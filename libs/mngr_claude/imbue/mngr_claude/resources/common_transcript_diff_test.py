"""Native-vs-common transcript diff for the claude converter (invariant U5).

Enumerates every user-visible turn straight from the real claude-code 2.1.207
session fixture and diffs that enumeration against what ``convert`` emits:
every turn must appear exactly once, in timestamp order, with each tool result
paired to its call and labeled with the call's real tool name. Schema validity
is deliberately NOT the assertion here -- a schema-valid transcript that
dropped a turn or labeled a result "unknown" (audit P2.14) validates fine but
fails this diff.

The enumeration re-encodes the legitimately-excluded native shapes as explicit
allowlists, independent of the converter's own filters: an unlisted session
record type, message role, or content block from a future binary -- or a
converter filter that broadens to swallow genuine turns -- fails the diff
loudly instead of hiding inside a vague filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_claude.resources import common_transcript_convert

_REAL_SESSION_FIXTURE = Path(__file__).parent / "test_fixtures" / "claude_session_slice.jsonl"

# Native session record types that are bookkeeping, never conversation turns.
# An unlisted type fails the enumeration, so additions must be deliberate.
_EXCLUDED_RECORD_TYPES = {
    # Session-mode marker (normal/plan/...); carries no uuid or content.
    "mode",
    # Tool/subtask progress stream.
    "progress",
    # Editor file-history snapshots.
    "file-history-snapshot",
    # System bookkeeping records.
    "system",
    # Turn-summary bookkeeping.
    "result",
}

# Slash-command plumbing: a typed ``/foo`` is recorded as expansion tags, local
# stdout, and an isMeta caveat wrapper -- none of which is a conversation turn.
# Re-encoded here (not imported from the converter) so a converter filter that
# broadens to swallow genuine turns fails the diff. Anchored on the LEADING
# tag: a turn that merely quotes the markup mid-text is a genuine turn.
_COMMAND_PLUMBING_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
)

# Assistant content block types that carry no transcript-visible content; a
# message left with no visible block at all is not a turn (thinking-only).
_EXCLUDED_ASSISTANT_BLOCK_TYPES = {
    "thinking",
    "redacted_thinking",
}


def _is_command_plumbing(text: str) -> bool:
    return text.lstrip().startswith(_COMMAND_PLUMBING_PREFIXES)


def _extract_user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        pytest.fail(f"native user content is neither string nor list: {content!r}")
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts)


def _tool_result_output(block: dict[str, Any]) -> str:
    raw_result = block.get("content", "")
    if isinstance(raw_result, str):
        output_text = raw_result
    elif isinstance(raw_result, list):
        parts = []
        for item in raw_result:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                # Non-text result block (image, etc.): no text to extract.
                continue
        output_text = "\n".join(parts)
    else:
        output_text = str(raw_result)
    return common_transcript_convert._truncate(output_text, common_transcript_convert._MAX_OUTPUT_LENGTH)


def _native_tool_names_by_call_id(records: list[dict[str, Any]]) -> dict[str, str]:
    """The tool name of every native tool_use block, keyed by its call id."""
    names: dict[str, str] = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                names[block["id"]] = block.get("name", "")
    return names


# A turn descriptor. Kinds: ("user", text), ("assistant", (text, calls)),
# ("tool_result", (call_id, tool_name, output, is_error)), and
# ("meta_result", text) for non-plumbing framework-injected isMeta messages,
# which legitimately surface under the tool role rather than as user turns.
# claude preserves native tool_use ids end to end, so descriptor equality
# covers call/result pairing directly.
def _enumerate_native_turns(records: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Classify every native session record as user-visible turn(s) or an explicitly excluded shape."""
    tool_names = _native_tool_names_by_call_id(records)
    dated_turns: list[tuple[str, tuple[str, Any]]] = []
    for record in records:
        record_type = record.get("type")
        if record_type in _EXCLUDED_RECORD_TYPES:
            continue
        if record_type not in ("user", "assistant"):
            pytest.fail(
                f"unclassified native session record type {record_type!r}: classify it as a turn or exclude it deliberately"
            )
        uuid = record.get("uuid", "")
        timestamp = record.get("timestamp", "")
        message = record.get("message")
        if not uuid or not timestamp or not isinstance(message, dict):
            pytest.fail(f"native {record_type} record missing uuid/timestamp/message: {record!r}")
        if record_type == "assistant":
            text_parts: list[str] = []
            calls: list[tuple[str, str, str]] = []
            for block in message.get("content", []):
                if not isinstance(block, dict):
                    pytest.fail(f"non-dict assistant content block: {block!r}")
                block_type = block.get("type", "")
                if block_type == "text":
                    if block.get("text"):
                        text_parts.append(block["text"])
                elif block_type == "tool_use":
                    preview = common_transcript_convert._truncate(
                        json.dumps(block.get("input", {}), separators=(",", ":")),
                        common_transcript_convert._MAX_INPUT_PREVIEW_LENGTH,
                    )
                    calls.append((block.get("id", ""), block.get("name", ""), preview))
                elif block_type in _EXCLUDED_ASSISTANT_BLOCK_TYPES:
                    # Excluded: model thinking is never transcript-visible.
                    continue
                else:
                    pytest.fail(
                        f"unclassified assistant content block type {block_type!r}: classify it or exclude it deliberately"
                    )
            if not text_parts and not calls:
                # Excluded: a thinking-only message has no visible content and
                # would otherwise render as a "(no content)" turn.
                continue
            dated_turns.append((timestamp, ("assistant", ("\n".join(text_parts), tuple(calls)))))
        else:
            is_meta = bool(record.get("isMeta", False))
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            tool_result_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            has_only_tool_results = (
                bool(tool_result_blocks)
                and all(b.get("type") == "tool_result" for b in blocks if isinstance(b, dict))
                and not any(isinstance(b, str) for b in blocks)
            )
            if not has_only_tool_results:
                text = _extract_user_text(content)
                if _is_command_plumbing(text):
                    # Excluded: slash-command plumbing is not a conversation turn.
                    pass
                elif not text:
                    # Excluded: an empty user message carries no signal.
                    pass
                elif is_meta:
                    # Framework-injected message: surfaces under the tool role,
                    # never as a user turn.
                    output = common_transcript_convert._truncate(text, common_transcript_convert._MAX_OUTPUT_LENGTH)
                    dated_turns.append((timestamp, ("meta_result", output)))
                else:
                    dated_turns.append((timestamp, ("user", text)))
            for block in tool_result_blocks:
                call_id = block.get("tool_use_id", "")
                if not call_id:
                    pytest.fail(f"native tool_result block without tool_use_id: {block!r}")
                if call_id not in tool_names:
                    pytest.fail(f"native tool_result pairs to no tool_use in the fixture: {call_id!r}")
                dated_turns.append(
                    (
                        timestamp,
                        (
                            "tool_result",
                            (
                                call_id,
                                tool_names[call_id],
                                _tool_result_output(block),
                                bool(block.get("is_error", False)),
                            ),
                        ),
                    )
                )
    # The converter orders its output by timestamp (stable), so the expected
    # side mirrors that ordering.
    dated_turns.sort(key=lambda dated: dated[0])
    return [turn for _, turn in dated_turns]


def _normalize_emitted(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    normalized: list[tuple[str, Any]] = []
    for event in events:
        event_type = event["type"]
        if event_type == "user_message":
            normalized.append(("user", event["content"]))
        elif event_type == "assistant_message":
            calls = tuple(
                (call["tool_call_id"], call["tool_name"], call["input_preview"]) for call in event["tool_calls"]
            )
            normalized.append(("assistant", (event["text"], calls)))
        elif event_type == "tool_result":
            if event["tool_name"] == "meta":
                normalized.append(("meta_result", event["output"]))
            else:
                normalized.append(
                    ("tool_result", (event["tool_call_id"], event["tool_name"], event["output"], event["is_error"]))
                )
        else:
            pytest.fail(f"unexpected common-transcript record type: {event_type!r}")
    return normalized


def _load_native_records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _REAL_SESSION_FIXTURE.read_text().splitlines() if line.strip()]


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_every_native_turn_appears_in_common_transcript_exactly_once(tmp_path: Path) -> None:
    """The native-vs-common diff over the real session slice: no drops, no
    duplicates, timestamp order preserved, every tool result paired to its
    call under the call's real tool name."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text(_REAL_SESSION_FIXTURE.read_text())
    common_transcript_convert.convert(str(input_file), str(output_file))
    events = _events(output_file)

    expected = _enumerate_native_turns(_load_native_records())
    actual = _normalize_emitted(events)

    # Guard against a degenerate enumeration: the captured session ran tools
    # and had a genuine typed turn, so the expected side must contain both.
    assert any(kind == "tool_result" for kind, _ in expected)
    assert any(kind == "user" for kind, _ in expected)

    # Exactly once, in order, with the same content and pairing (claude
    # preserves native tool_use ids, so equality covers call/result pairing;
    # a result labeled "unknown" also fails here).
    assert actual == expected

    assert len({event["event_id"] for event in events}) == len(events)
    # Schema validity is necessary (but alone would not have caught the drift).
    for event in events:
        assert validate_common_transcript_record(event) is None, event


def test_reconverting_the_same_session_appends_nothing(tmp_path: Path) -> None:
    """Exactly-once across passes: re-running convert over the same session
    must not duplicate any turn."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text(_REAL_SESSION_FIXTURE.read_text())
    assert common_transcript_convert.convert(str(input_file), str(output_file)) > 0
    content_after_first_pass = output_file.read_text()
    assert common_transcript_convert.convert(str(input_file), str(output_file)) == 0
    assert output_file.read_text() == content_after_first_pass
