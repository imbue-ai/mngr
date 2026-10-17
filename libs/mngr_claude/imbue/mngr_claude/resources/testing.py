"""Shared builders and readers for the claude common-transcript tests.

The converter's unit tests and the shell-level tests describe the same raw Claude
transcript shapes and read back the same emitted stream, so the builders live here
once. Builders return plain dicts; the shell tests JSON-encode them into the raw
input file (``write_raw_transcript`` does that, and also passes through raw strings
for the deliberately malformed lines).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Distinct default timestamps for the three record kinds, so a user turn, the
# assistant reply it prompts, and the tool result that follows sort in that order
# without every test spelling out timestamps.
DEFAULT_USER_TIMESTAMP = "2026-01-01T00:00:00Z"
DEFAULT_ASSISTANT_TIMESTAMP = "2026-01-01T00:00:01Z"
DEFAULT_TOOL_RESULT_TIMESTAMP = "2026-01-01T00:00:02Z"

DEFAULT_MODEL = "claude-opus-4-8"

# Claude reports input tokens in three counters; ATIF's prompt_tokens is their sum
# (DEFAULT_PROMPT_TOKENS), with the cache read alone as cached_tokens.
DEFAULT_USAGE: Mapping[str, int] = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 80,
    "cache_creation_input_tokens": 20,
}
DEFAULT_PROMPT_TOKENS = 200


def make_assistant_record(
    uuid: str,
    *,
    text: str = "",
    thinking: str | None = None,
    tool_uses: Sequence[Mapping[str, Any]] | None = None,
    timestamp: str = DEFAULT_ASSISTANT_TIMESTAMP,
    message_id: str | None = None,
    model: str = DEFAULT_MODEL,
    stop_reason: str = "end_turn",
    usage: Mapping[str, int] | None = None,
    is_sidechain: bool = False,
) -> dict[str, Any]:
    """Build one raw assistant line.

    ``message_id`` defaults to one derived from the line's own uuid, so separate
    calls describe separate inferences; pass the same id to two calls to describe
    one API response fanned out over two lines, as claude records it.
    """
    content: list[dict[str, Any]] = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig"})
    if text:
        content.append({"type": "text", "text": text})
    for tool_use in tool_uses or []:
        content.append(
            {
                "type": "tool_use",
                "id": tool_use["id"],
                "name": tool_use["name"],
                "input": tool_use.get("input", {}),
            }
        )
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": is_sidechain,
        "message": {
            "role": "assistant",
            "id": message_id or f"msg_{uuid}",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "usage": dict(DEFAULT_USAGE if usage is None else usage),
        },
    }


def make_user_record(
    uuid: str,
    *,
    text: str = "",
    tool_results: Sequence[Mapping[str, Any]] | None = None,
    timestamp: str = DEFAULT_USER_TIMESTAMP,
    is_meta: bool = False,
    is_sidechain: bool = False,
) -> dict[str, Any]:
    """Build one raw user line: a typed turn, framework-injected content, or both.

    A plain typed turn is recorded with string content, as claude does; anything
    carrying tool results is recorded as a content-block list. ``is_meta=True``
    marks content Claude Code injected itself (stop hook output, command caveats).
    """
    content: str | list[dict[str, Any]]
    if text and not tool_results:
        content = text
    else:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tool_result in tool_results or []:
            blocks.append({"type": "tool_result", **tool_result})
        content = blocks
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": timestamp,
        "isMeta": is_meta,
        "isSidechain": is_sidechain,
        "message": {"role": "user", "content": content},
    }


def make_tool_result_record(
    uuid: str,
    tool_use_id: str,
    output: Any,
    *,
    is_error: bool = False,
    timestamp: str = DEFAULT_TOOL_RESULT_TIMESTAMP,
    is_sidechain: bool = False,
) -> dict[str, Any]:
    """Build the raw user line that carries one tool's result back to the model."""
    return make_user_record(
        uuid,
        tool_results=[{"tool_use_id": tool_use_id, "content": output, "is_error": is_error}],
        timestamp=timestamp,
        is_sidechain=is_sidechain,
    )


def write_raw_transcript(input_file: Path, records: Sequence[Mapping[str, Any] | str]) -> None:
    """Write raw transcript lines: built records, or verbatim strings for bad input."""
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    input_file.write_text("\n".join(lines) + "\n" if lines else "")


def read_stream(output_file: Path) -> list[dict[str, Any]]:
    """Parse the emitted common-transcript stream ([] when nothing was emitted)."""
    if not output_file.exists():
        return []
    return [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]


def read_steps(output_file: Path, source: str) -> list[dict[str, Any]]:
    """The emitted step records of one ATIF source ("user", "agent", "system")."""
    return [event for event in read_stream(output_file) if event["type"] == "step" and event["source"] == source]


def read_observations(output_file: Path) -> list[dict[str, Any]]:
    """The emitted observation records (streamed tool results)."""
    return [event for event in read_stream(output_file) if event["type"] == "observation"]
