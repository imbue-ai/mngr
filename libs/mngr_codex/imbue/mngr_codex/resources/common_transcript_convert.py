#!/usr/bin/env python3
"""Common-transcript converter for codex agents (invoked by common_transcript.sh).

Reads the raw codex rollout stream (``logs/codex_transcript/events.jsonl``,
produced verbatim by stream_transcript.sh) and appends the semantically
important rollout items in the agent-agnostic common format to
``events/codex/common_transcript/events.jsonl``.

codex rollout wire shape (verified live against codex 0.64.0 and the patched
codex 0.146.0 build):
  {"timestamp":"<ISO8601>","type":<t>,"payload":<p>}
with the item kinds this converter cares about carried under type
"response_item":
  payload.type=="message", role=="user"      -> user_message
  payload.type=="message", role=="assistant" -> assistant_message
  payload.type=="function_call"              -> assistant_message (tool_calls
                                    attached); also remembered by payload.call_id
  payload.type=="function_call_output"       -> tool_result, paired by call_id
  payload.type=="custom_tool_call"           -> assistant_message (tool_calls
                                    attached); the 0.146 unified exec tool emits
                                    these (payload.input, not .arguments)
  payload.type=="custom_tool_call_output"    -> tool_result, paired by call_id

User-role messages that are instruction injections rather than genuine user
turns -- the AGENTS.md context blob ("# AGENTS.md instructions for <dir>" with
an "<INSTRUCTIONS>" envelope) and codex's own "<user_instructions>" /
"<environment_context>" initial-context items -- are skipped so they never
surface as user_message records.

codex models a tool invocation as its own rollout item, separate from the
assistant's reasoning text (a distinct ``message`` item), so the call is emitted
as a standalone assistant_message carrying the tool_call -- matching the other
ports, whose native formats nest tool_calls in the assistant message.

Event ids are synthesized by hashing the line's timestamp and content (plus the
item kind), so re-processing the same input never produces duplicates and ids
never repeat across agents or hosts; the converter also dedupes against the set
of event_ids already in the output file.

Invoked as ``python3 common_transcript_convert.py`` with the input/output paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` environment variables that
common_transcript.sh sets. Malformed or null lines are dropped silently; only an
uncaught exception writes to stderr, which the shell reports as a convert error
(the count of appended events is printed to stdout for common_transcript.sh to
capture). Split out of
the shell script (it used to be an inline ``python3`` heredoc) so the logic is
lintable, type-checked, and unit-testable directly rather than only through a
subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from typing import Union

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

_MAX_INPUT_PREVIEW_LENGTH = 200
_MAX_OUTPUT_LENGTH = 2000
_SOURCE = "codex/common_transcript"

# codex wraps its own initial-context items in these envelopes and carries them
# as user-role messages; they are plumbing, not user turns.
_CONTEXT_INJECTION_PREFIXES = ("<user_instructions>", "<environment_context>")
# The AGENTS.md context injection (seen live on codex 0.146.0): a user-role
# message opening with this header, with the file body in an <INSTRUCTIONS> envelope.
_AGENTS_MD_INJECTION_PREFIX = "# AGENTS.md instructions for "
_AGENTS_MD_INJECTION_ENVELOPE = "<INSTRUCTIONS>"


def _is_injected_instructions(text: str) -> bool:
    """True for instruction-injection messages that must not surface as user turns."""
    stripped = text.lstrip()
    if stripped.startswith(_AGENTS_MD_INJECTION_PREFIX) and _AGENTS_MD_INJECTION_ENVELOPE in stripped:
        return True
    return stripped.startswith(_CONTEXT_INJECTION_PREFIXES)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _join_content_text(content: JsonValue, item_type: str) -> str:
    """Join the .text of payload.content[] items whose type matches item_type."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != item_type:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _stringify_output(output: JsonValue) -> str:
    """Render a tool output payload (.output), which is a string OR a content array."""
    if isinstance(output, str):
        return output
    # An array of content items: join the text of each, falling back to a JSON
    # dump of any item that doesn't carry a plain .text field.
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, separators=(",", ":")))
        return "".join(parts)
    # Anything else (a bare object/number): render it as JSON so nothing is lost.
    return json.dumps(output, separators=(",", ":"))


def _load_existing_ids(output_file: str) -> set[str]:
    ids: set[str] = set()
    if not os.path.isfile(output_file):
        return ids
    with open(output_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def _make_event_id(timestamp: str, content: str, kind: str) -> str:
    """A stable, globally unique event id from the record's own timestamp and content.

    Line-index ids repeat identically for every agent on every host (analytics
    dedupes transcripts fleet-wide by event id); hashing the line's timestamp
    plus content keeps re-processing idempotent while making ids unique.
    """
    digest = hashlib.sha256(f"{timestamp}:{content[:1024]}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"evt-{digest}-{kind}"


def _is_already_converted(existing_ids: set[str], event_id: str, line_index: int, kind: str) -> bool:
    # CLEANUP: drop the legacy line-index id check once transcripts converted
    # before the content-hash ids existed have aged out of live agent hosts.
    return event_id in existing_ids or f"line-{line_index}-{kind}" in existing_ids


def convert(input_file: str, output_file: str) -> int:
    """Append new common-transcript events from ``input_file`` to ``output_file``; return the count."""
    existing_ids = _load_existing_ids(output_file)
    if not os.path.isfile(input_file):
        return 0

    new_events: list[tuple[str, int, dict[str, Any]]] = []
    # Pending function calls awaiting their output, keyed by call_id. Each value
    # carries the synthetic tool_call_id, the tool name, and the input preview.
    pending_call_by_id: dict[str, dict[str, Any]] = {}

    with open(input_file, encoding="utf-8", errors="replace") as f:
        for line_index, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            # Ignore event_msg entirely (display duplicates of response_items).
            if raw.get("type") != "response_item":
                continue
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue

            timestamp = raw.get("timestamp", "")
            payload_type = payload.get("type")

            if payload_type == "message" and payload.get("role") == "user":
                text = _join_content_text(payload.get("content"), "input_text")
                # An empty user message carries no signal -> drop it.
                if not text:
                    continue
                # Instruction injections ride in as user-role messages but are
                # not user turns -> drop them (export-side filtering only).
                if _is_injected_instructions(text):
                    continue
                event_id = _make_event_id(timestamp, text, "user")
                if _is_already_converted(existing_ids, event_id, line_index, "user"):
                    continue
                existing_ids.add(event_id)
                new_events.append(
                    (
                        timestamp,
                        line_index,
                        {
                            "timestamp": timestamp,
                            "type": "user_message",
                            "event_id": event_id,
                            "source": _SOURCE,
                            "role": "user",
                            "content": text,
                        },
                    )
                )

            elif payload_type == "message" and payload.get("role") == "assistant":
                text = _join_content_text(payload.get("content"), "output_text")
                event_id = _make_event_id(timestamp, text, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    continue
                existing_ids.add(event_id)
                # codex assistant messages are text-only (tool calls are separate
                # response_items surfaced as tool_results), so parts is just the text and
                # its order is trivially faithful.
                parts = [{"type": "text", "content": text}] if text else []
                new_events.append(
                    (
                        timestamp,
                        line_index,
                        {
                            "timestamp": timestamp,
                            "type": "assistant_message",
                            "event_id": event_id,
                            "source": _SOURCE,
                            "role": "assistant",
                            "model": None,
                            "text": text,
                            "tool_calls": [],
                            "parts": parts,
                            "parts_ordered": True,
                            "finish_reason": None,
                            "usage": None,
                        },
                    )
                )

            elif payload_type in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                name = payload.get("name", "")
                # A custom_tool_call (the 0.146 unified exec tool) carries its
                # invocation under "input"; a function_call under "arguments".
                if payload_type == "custom_tool_call":
                    arguments = payload.get("input", "")
                else:
                    arguments = payload.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, separators=(",", ":"))
                tool_call_id = f"line-{line_index}-tc"
                tool_name = name if isinstance(name, str) else ""
                input_preview = _truncate(arguments, _MAX_INPUT_PREVIEW_LENGTH)
                tool_call = {
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "input_preview": input_preview,
                }
                pending_call_by_id[call_id] = tool_call
                # Emit the invocation on the assistant turn (see module docstring): codex
                # carries no assistant `message` for a tool call, so without this the
                # canonical envelope would record the tool_result with no assistant
                # tool_call. The tool_call_id matches the paired tool_result below. Each
                # call is its own rollout item, so the single tool_call part is trivially
                # ordered -> parts_ordered=True.
                event_id = _make_event_id(timestamp, arguments, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    continue
                existing_ids.add(event_id)
                new_events.append(
                    (
                        timestamp,
                        line_index,
                        {
                            "timestamp": timestamp,
                            "type": "assistant_message",
                            "event_id": event_id,
                            "source": _SOURCE,
                            "role": "assistant",
                            "model": None,
                            "text": "",
                            "tool_calls": [tool_call],
                            "parts": [{"type": "tool_call", **tool_call}],
                            "parts_ordered": True,
                            "finish_reason": None,
                            "usage": None,
                        },
                    )
                )

            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                pending = pending_call_by_id.pop(call_id, None) if isinstance(call_id, str) else None
                # An output with no matching call has
                # nothing to pair with -> drop it.
                if pending is None:
                    continue
                output = _truncate(_stringify_output(payload.get("output", "")), _MAX_OUTPUT_LENGTH)
                event_id = _make_event_id(timestamp, output, "tool_result")
                if _is_already_converted(existing_ids, event_id, line_index, "tool_result"):
                    continue
                existing_ids.add(event_id)
                new_events.append(
                    (
                        timestamp,
                        line_index,
                        {
                            "timestamp": timestamp,
                            "type": "tool_result",
                            "event_id": event_id,
                            "source": _SOURCE,
                            "tool_call_id": pending["tool_call_id"],
                            "tool_name": pending["tool_name"],
                            "output": output,
                            "is_error": False,
                        },
                    )
                )

            else:
                # Other payload types (reasoning, web_search_call, ...) and
                # non-user/assistant message roles (developer instruction
                # injections) are bookkeeping, not conversation content.
                continue

    if not new_events:
        return 0

    # Stable order: by line index (the append-only stream order), which also
    # keeps tool_results after their originating call.
    new_events.sort(key=lambda triple: triple[1])
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for _, _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    return len(new_events)


if __name__ == "__main__":
    print(convert(os.environ["_INPUT_FILE"], os.environ["_OUTPUT_FILE"]))
