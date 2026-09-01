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
loudly instead of hiding inside a vague filter. It also re-encodes the
inference grouping (all assistant lines of one lane sharing a ``message.id`` are
one ATIF step) in its own simpler form, so a converter that split one inference
into two steps -- double-counting its tokens -- fails here. A native Task
subagent writes into the same session file marked ``isSidechain``, so the
enumeration reads each turn's lane too: a lane confusion (a subagent's turn
attributed to the main thread, or its records splitting a main-thread inference)
fails the diff.
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

# Assistant content block types that carry no readable content at all: redacted
# thinking is an opaque blob, so it contributes neither message nor reasoning.
_EXCLUDED_ASSISTANT_BLOCK_TYPES = {
    "redacted_thinking",
}

# A turn descriptor: its kind, its content, and its lane (True for a native Task
# subagent's records, which claude interleaves into the same file). Kinds:
# ("user", text), ("system", text) for framework-injected isMeta messages and
# compaction boundaries, ("agent", (message, reasoning, calls)), and
# ("observation", (call_id, tool_name, output, is_error)). claude preserves native
# tool_use ids end to end, so descriptor equality covers call/result pairing.
_Turn = tuple[str, Any, bool]


def _is_sidechain(record: dict[str, Any]) -> bool:
    return bool(record.get("isSidechain"))


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
        return raw_result
    if isinstance(raw_result, list):
        parts = []
        for item in raw_result:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
            else:
                # Non-text result block (image, etc.): no text to extract.
                continue
        return "\n".join(parts)
    return str(raw_result)


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


def _enumerate_native_agent_steps(records: list[dict[str, Any]]) -> list[tuple[str, _Turn]]:
    """One dated turn descriptor per native inference (a lane's lines sharing a message.id)."""
    by_lane_and_message_id: dict[tuple[bool, str], dict[str, Any]] = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record["message"]
        message_id = message.get("id")
        if not message_id:
            pytest.fail(f"native assistant record without message.id: {record!r}")
        inference = by_lane_and_message_id.setdefault(
            (_is_sidechain(record), message_id),
            {
                "timestamp": record["timestamp"],
                "is_sidechain": _is_sidechain(record),
                "text": [],
                "reasoning": [],
                "calls": [],
            },
        )
        for block in message.get("content", []):
            if not isinstance(block, dict):
                pytest.fail(f"non-dict assistant content block: {block!r}")
            block_type = block.get("type", "")
            if block_type == "text":
                if block.get("text"):
                    inference["text"].append(block["text"])
            elif block_type == "thinking":
                if block.get("thinking"):
                    inference["reasoning"].append(block["thinking"])
            elif block_type == "tool_use":
                inference["calls"].append((block.get("id", ""), block.get("name", ""), block.get("input", {})))
            elif block_type in _EXCLUDED_ASSISTANT_BLOCK_TYPES:
                # Excluded: an opaque blob with nothing to render.
                continue
            else:
                pytest.fail(
                    f"unclassified assistant content block type {block_type!r}: classify it or exclude it deliberately"
                )
    dated: list[tuple[str, _Turn]] = []
    for inference in by_lane_and_message_id.values():
        if not inference["text"] and not inference["reasoning"] and not inference["calls"]:
            # Excluded: an inference with nothing said, thought, or done would
            # render as an empty turn.
            continue
        dated.append(
            (
                inference["timestamp"],
                (
                    "agent",
                    (
                        "\n".join(inference["text"]),
                        "\n\n".join(inference["reasoning"]),
                        tuple(inference["calls"]),
                    ),
                    inference["is_sidechain"],
                ),
            )
        )
    return dated


def _enumerate_native_turns(records: list[dict[str, Any]]) -> list[_Turn]:
    """Classify every native session record as user-visible turn(s) or an explicitly excluded shape."""
    tool_names = _native_tool_names_by_call_id(records)
    dated_turns: list[tuple[str, _Turn]] = _enumerate_native_agent_steps(records)
    for record in records:
        record_type = record.get("type")
        if record_type in _EXCLUDED_RECORD_TYPES:
            continue
        if record_type not in ("user", "assistant"):
            pytest.fail(
                f"unclassified native session record type {record_type!r}: classify it or exclude it deliberately"
            )
        uuid = record.get("uuid", "")
        timestamp = record.get("timestamp", "")
        is_sidechain = _is_sidechain(record)
        message = record.get("message")
        if not uuid or not timestamp or not isinstance(message, dict):
            pytest.fail(f"native {record_type} record missing uuid/timestamp/message: {record!r}")
        if record_type == "assistant":
            # Already enumerated above, grouped by inference rather than by line.
            continue
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
            if record.get("isCompactSummary"):
                # A compaction boundary is a system-initiated operation, not a turn
                # the human typed; its summary rides on the step's own observation.
                dated_turns.append((timestamp, ("system", "Context compaction performed", is_sidechain)))
            elif _is_command_plumbing(text):
                # Excluded: slash-command plumbing is not a conversation turn.
                pass
            elif not text:
                # Excluded: an empty user message carries no signal.
                pass
            elif record.get("isMeta"):
                # Framework-injected message: a system step, never a user turn.
                dated_turns.append((timestamp, ("system", text, is_sidechain)))
            else:
                dated_turns.append((timestamp, ("user", text, is_sidechain)))
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
                        "observation",
                        (
                            call_id,
                            tool_names[call_id],
                            _tool_result_output(block),
                            bool(block.get("is_error", False)),
                        ),
                        is_sidechain,
                    ),
                )
            )
    # The converter orders its output by timestamp (stable), so the expected
    # side mirrors that ordering.
    dated_turns.sort(key=lambda dated: dated[0])
    return [turn for _, turn in dated_turns]


def _normalize_emitted(events: list[dict[str, Any]]) -> list[_Turn]:
    normalized: list[_Turn] = []
    for event in events:
        event_type = event["type"]
        if event_type == "header":
            # Stream framing, not a conversation turn.
            continue
        if event_type == "step":
            source = event["source"]
            # The emitted lane marker is present only on the sidechain lane.
            is_sidechain = bool((event.get("extra") or {}).get("is_sidechain", False))
            if source == "agent":
                calls = tuple(
                    (call["tool_call_id"], call["function_name"], call["arguments"])
                    for call in event.get("tool_calls", [])
                )
                normalized.append(
                    ("agent", (event["message"], event.get("reasoning_content") or "", calls), is_sidechain)
                )
            else:
                normalized.append((source, event["message"], is_sidechain))
        elif event_type == "observation":
            for result in event["results"]:
                extra = result["extra"]
                normalized.append(
                    (
                        "observation",
                        (result["source_call_id"], extra["tool_name"], result["content"], extra["is_error"]),
                        bool(extra.get("is_sidechain", False)),
                    )
                )
        else:
            pytest.fail(f"unexpected common-transcript record type: {event_type!r}")
    return normalized


def _load_native_records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _REAL_SESSION_FIXTURE.read_text().splitlines() if line.strip()]


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _convert_complete(input_file: Path, output_file: Path) -> int:
    """Convert the fixture as a turn-end flush does: the session is over, so nothing
    is still being appended and no inference is held back (the mid-turn deferral has
    its own tests in common_transcript_convert_test.py)."""
    return common_transcript_convert.convert(str(input_file), str(output_file), is_input_complete=True)


def test_every_native_turn_appears_in_common_transcript_exactly_once(tmp_path: Path) -> None:
    """The native-vs-common diff over the real session slice: no drops, no
    duplicates, timestamp order preserved, every tool result paired to its
    call under the call's real tool name."""
    input_file, output_file = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    input_file.write_text(_REAL_SESSION_FIXTURE.read_text())
    _convert_complete(input_file, output_file)
    events = _events(output_file)

    expected = _enumerate_native_turns(_load_native_records())
    actual = _normalize_emitted(events)

    # Guard against a degenerate enumeration: the captured session ran tools
    # and had a genuine typed turn, so the expected side must contain both.
    assert any(kind == "observation" for kind, _, _ in expected)
    assert any(kind == "user" for kind, _, _ in expected)

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
    assert _convert_complete(input_file, output_file) > 0
    content_after_first_pass = output_file.read_text()
    assert _convert_complete(input_file, output_file) == 0
    assert output_file.read_text() == content_after_first_pass
