"""Native-vs-common transcript diff for the codex converter (invariant U5).

Enumerates every user-visible turn straight from a REAL rollout captured from
the patched codex-cli 0.146.0 build and diffs that enumeration against what
``convert`` emits: every turn must appear exactly once, in order, with each
tool call paired to its result. Schema validity is deliberately NOT the
assertion here -- a schema-valid transcript that silently dropped all tool
activity (the original 0.146 drift, audit P2.12) validates fine but fails
this diff.

The enumeration re-encodes the legitimately-excluded native shapes as explicit
allowlists, independent of the converter's own filters: a new rollout shape
from a future binary, or a converter filter that broadens to swallow genuine
turns, fails the diff loudly instead of hiding inside a vague filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_codex.resources import common_transcript_convert

_REAL_0146_ROLLOUT = Path(__file__).parent / "test_fixtures" / "codex_0146_rollout_exec_turn.jsonl"

# Rollout line types that are bookkeeping, never conversation turns. An
# unlisted line type fails the enumeration, so additions must be deliberate.
_EXCLUDED_LINE_TYPES = {
    # Session header: ids, cwd, base instructions.
    "session_meta",
    # Display-stream duplicates of response_items (user_message/agent_message)
    # plus progress markers (task_started, token_count, task_complete).
    "event_msg",
    # Environment/tooling snapshot bookkeeping.
    "world_state",
    # Per-turn model/config bookkeeping.
    "turn_context",
}

# Message roles that are harness plumbing, not conversation turns.
_EXCLUDED_MESSAGE_ROLES = {
    # Harness/system injections: codex-specific instructions, the multi-agent
    # preamble, step-tracking reminders.
    "developer",
}

# User-role messages that are instruction injections rather than typed turns:
# the AGENTS.md context blob and codex's own initial-context envelopes. They are
# turns in their own right (system steps carrying the session's configuration), so
# the enumeration classifies them rather than excluding them. Re-encoded here (not
# imported from the converter) so a converter filter that broadens to swallow
# genuine user turns fails the diff.
_AGENTS_MD_PREFIX = "# AGENTS.md instructions for "
_AGENTS_MD_ENVELOPE = "<INSTRUCTIONS>"
_CONTEXT_ENVELOPE_PREFIXES = ("<user_instructions>", "<environment_context>")


def _is_injected_user_context(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(_AGENTS_MD_PREFIX) and _AGENTS_MD_ENVELOPE in stripped:
        return True
    return stripped.startswith(_CONTEXT_ENVELOPE_PREFIXES)


def _joined_text(content: Any, item_type: str) -> str:
    if not isinstance(content, list):
        pytest.fail(f"native message content is not a list: {content!r}")
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == item_type and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


# A turn descriptor: (kind, pairing token, payload). The pairing token ties a
# tool call to its result; it is codex's native call_id on both sides because the
# emitter records that id verbatim as the ATIF tool_call_id / source_call_id, so
# the descriptors are compared literally.
#
# The payloads for reasoning, arguments, and tool output are built by calling the
# converter's own text helpers (_join_reasoning_text / _parse_arguments /
# _stringify_output), so this diff cannot detect a bug inside them. That is
# deliberate: the diff answers "is every native turn present, exactly once, in
# order, still paired with its result?", and the fidelity of each individual
# transformation is pinned by the converter's unit tests instead.
def _enumerate_native_turns(rollout_lines: list[str]) -> list[tuple[str, str, Any]]:
    """Classify every native rollout line as a user-visible turn or an explicitly excluded shape."""
    turns: list[tuple[str, str, Any]] = []
    for line in rollout_lines:
        record = json.loads(line)
        line_type = record.get("type")
        if line_type in _EXCLUDED_LINE_TYPES:
            continue
        if line_type != "response_item":
            pytest.fail(
                f"unclassified native rollout line type {line_type!r}: classify it as a turn or exclude it deliberately"
            )
        payload = record["payload"]
        payload_type = payload.get("type")
        if payload_type == "reasoning":
            reasoning = common_transcript_convert._join_reasoning_text(payload)
            if not reasoning:
                # Excluded: this build's reasoning items carry only an opaque
                # encrypted payload, so there is no thinking text to surface.
                continue
            turns.append(("reasoning", "", reasoning))
        elif payload_type == "message":
            role = payload.get("role")
            if role in _EXCLUDED_MESSAGE_ROLES:
                continue
            if role == "user":
                text = _joined_text(payload.get("content"), "input_text")
                if not text:
                    # Excluded: an empty user message carries no signal.
                    continue
                kind = "system" if _is_injected_user_context(text) else "user"
                turns.append((kind, "", text))
            elif role == "assistant":
                text = _joined_text(payload.get("content"), "output_text")
                if not text:
                    # Excluded: an assistant message with no text carries no signal
                    # (codex models a tool invocation as its own rollout item).
                    continue
                turns.append(("agent_text", "", text))
            else:
                pytest.fail(
                    f"unclassified native message role {role!r}: classify it as a turn or exclude it deliberately"
                )
        elif payload_type in ("custom_tool_call", "function_call"):
            invocation = payload["input"] if payload_type == "custom_tool_call" else payload["arguments"]
            arguments = common_transcript_convert._parse_arguments(invocation)
            turns.append(("tool_call", payload["call_id"], (payload["name"], arguments)))
        elif payload_type in ("custom_tool_call_output", "function_call_output"):
            output = common_transcript_convert._stringify_output(payload.get("output", ""))
            turns.append(("tool_result", payload["call_id"], output))
        else:
            pytest.fail(
                f"unclassified native rollout payload type {payload_type!r}: classify it as a turn or exclude it deliberately"
            )
    return turns


def _normalize_emitted(records: list[dict[str, Any]]) -> list[tuple[str, str, Any]]:
    normalized: list[tuple[str, str, Any]] = []
    for record in records:
        record_type = record["type"]
        if record_type == "header":
            # Stream framing, not a turn; its position is asserted separately.
            continue
        if record_type == "step":
            source = record["source"]
            if source == "user":
                normalized.append(("user", "", record["message"]))
            elif source == "system":
                normalized.append(("system", "", record["message"]))
            elif source == "agent":
                if record.get("tool_calls"):
                    if len(record["tool_calls"]) != 1 or record["message"]:
                        pytest.fail(f"codex emits each tool call as its own bare agent step, got: {record!r}")
                    call = record["tool_calls"][0]
                    normalized.append(("tool_call", call["tool_call_id"], (call["function_name"], call["arguments"])))
                elif record.get("reasoning_content"):
                    normalized.append(("reasoning", "", record["reasoning_content"]))
                else:
                    normalized.append(("agent_text", "", record["message"]))
            else:
                pytest.fail(f"unexpected step source: {source!r}")
        elif record_type == "observation":
            for result in record["results"]:
                normalized.append(("tool_result", result["source_call_id"], result["content"]))
        else:
            pytest.fail(f"unexpected common-transcript record type: {record_type!r}")
    return normalized


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_every_native_turn_appears_in_common_transcript_exactly_once(tmp_path: Path) -> None:
    """The native-vs-common diff over the real 0.146 rollout: no drops, no
    duplicates, order preserved, tool calls paired with their results."""
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file))
    records = _records(output_file)

    expected = _enumerate_native_turns(_REAL_0146_ROLLOUT.read_text().splitlines())
    actual = _normalize_emitted(records)

    # Guard against a degenerate enumeration: the captured turn ran a command,
    # so the expected side must itself contain tool activity and a user turn.
    assert any(kind == "tool_call" for kind, _, _ in expected)
    assert any(kind == "user" for kind, _, _ in expected)

    # Exactly once, in order, with the same content and the same native call ids
    # (drop, duplicate, reorder, and mispairing all fail).
    assert actual == expected

    assert records[0]["type"] == "header"
    assert len({record["event_id"] for record in records}) == len(records)
    # Schema validity is necessary (but alone would not have caught the drift).
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_reconverting_the_same_rollout_appends_nothing(tmp_path: Path) -> None:
    """Exactly-once across passes: re-running convert over the same rollout
    must not duplicate any turn (or re-write the header)."""
    output_file = tmp_path / "out.jsonl"
    assert common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file)) > 0
    content_after_first_pass = output_file.read_text()
    assert common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file)) == 0
    assert output_file.read_text() == content_after_first_pass
