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

# response_item payload types that carry no user-visible turn.
_EXCLUDED_PAYLOAD_TYPES = {
    # Model thinking; never surfaced in the transcript.
    "reasoning",
}

# Message roles that are instruction injections, not conversation turns.
_EXCLUDED_MESSAGE_ROLES = {
    # Harness/system injections: codex-specific instructions, the multi-agent
    # preamble, step-tracking reminders.
    "developer",
}

# User-role messages that are instruction injections rather than typed turns:
# the AGENTS.md context blob and codex's own initial-context envelopes.
# Re-encoded here (not imported from the converter) so a converter filter that
# broadens to swallow genuine user turns fails the diff.
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
# tool call to its result (native call_id on the expected side, emitted
# tool_call_id on the actual side); it is compared structurally, not literally,
# because the converter synthesizes its own ids.
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
        if payload_type in _EXCLUDED_PAYLOAD_TYPES:
            continue
        if payload_type == "message":
            role = payload.get("role")
            if role in _EXCLUDED_MESSAGE_ROLES:
                continue
            if role == "user":
                text = _joined_text(payload.get("content"), "input_text")
                if not text:
                    # Excluded: an empty user message carries no signal.
                    continue
                if _is_injected_user_context(text):
                    # Excluded: instruction injection riding in as a user-role message.
                    continue
                turns.append(("user", "", text))
            elif role == "assistant":
                turns.append(("assistant_text", "", _joined_text(payload.get("content"), "output_text")))
            else:
                pytest.fail(
                    f"unclassified native message role {role!r}: classify it as a turn or exclude it deliberately"
                )
        elif payload_type in ("custom_tool_call", "function_call"):
            invocation = payload["input"] if payload_type == "custom_tool_call" else payload["arguments"]
            preview = common_transcript_convert._truncate(
                invocation, common_transcript_convert._MAX_INPUT_PREVIEW_LENGTH
            )
            turns.append(("tool_call", payload["call_id"], (payload["name"], preview)))
        elif payload_type in ("custom_tool_call_output", "function_call_output"):
            output = common_transcript_convert._truncate(
                common_transcript_convert._stringify_output(payload.get("output", "")),
                common_transcript_convert._MAX_OUTPUT_LENGTH,
            )
            turns.append(("tool_result", payload["call_id"], output))
        else:
            pytest.fail(
                f"unclassified native rollout payload type {payload_type!r}: classify it as a turn or exclude it deliberately"
            )
    return turns


def _normalize_emitted(events: list[dict[str, Any]]) -> list[tuple[str, str, Any]]:
    normalized: list[tuple[str, str, Any]] = []
    for event in events:
        event_type = event["type"]
        if event_type == "user_message":
            normalized.append(("user", "", event["content"]))
        elif event_type == "assistant_message":
            if event["tool_calls"]:
                if len(event["tool_calls"]) != 1 or event["text"]:
                    pytest.fail(f"codex emits each tool call as its own bare assistant record, got: {event!r}")
                call = event["tool_calls"][0]
                normalized.append(("tool_call", call["tool_call_id"], (call["tool_name"], call["input_preview"])))
            else:
                normalized.append(("assistant_text", "", event["text"]))
        elif event_type == "tool_result":
            normalized.append(("tool_result", event["tool_call_id"], event["output"]))
        else:
            pytest.fail(f"unexpected common-transcript record type: {event_type!r}")
    return normalized


def _pairing_groups(turns: list[tuple[str, str, Any]]) -> list[tuple[int, ...]]:
    """Positions that share a pairing token, i.e. which tool_call goes with which tool_result."""
    positions_by_token: dict[str, list[int]] = {}
    for index, (kind, token, _) in enumerate(turns):
        if kind in ("tool_call", "tool_result"):
            positions_by_token.setdefault(token, []).append(index)
    return sorted(tuple(positions) for positions in positions_by_token.values())


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_every_native_turn_appears_in_common_transcript_exactly_once(tmp_path: Path) -> None:
    """The native-vs-common diff over the real 0.146 rollout: no drops, no
    duplicates, order preserved, tool calls paired with their results."""
    output_file = tmp_path / "out.jsonl"
    common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file))
    events = _events(output_file)

    expected = _enumerate_native_turns(_REAL_0146_ROLLOUT.read_text().splitlines())
    actual = _normalize_emitted(events)

    # Guard against a degenerate enumeration: the captured turn ran a command,
    # so the expected side must itself contain tool activity and a user turn.
    assert any(kind == "tool_call" for kind, _, _ in expected)
    assert any(kind == "user" for kind, _, _ in expected)

    # Exactly once, in order, with the same content (drop, duplicate, and
    # reorder all fail); pairing is compared structurally since the converter
    # synthesizes its own tool_call ids.
    assert [(kind, payload) for kind, _, payload in actual] == [(kind, payload) for kind, _, payload in expected]
    assert _pairing_groups(actual) == _pairing_groups(expected)

    assert len({event["event_id"] for event in events}) == len(events)
    # Schema validity is necessary (but alone would not have caught the drift).
    for event in events:
        assert validate_common_transcript_record(event) is None, event


def test_reconverting_the_same_rollout_appends_nothing(tmp_path: Path) -> None:
    """Exactly-once across passes: re-running convert over the same rollout
    must not duplicate any turn."""
    output_file = tmp_path / "out.jsonl"
    assert common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file)) > 0
    content_after_first_pass = output_file.read_text()
    assert common_transcript_convert.convert(str(_REAL_0146_ROLLOUT), str(output_file)) == 0
    assert output_file.read_text() == content_after_first_pass
