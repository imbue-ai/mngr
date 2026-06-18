#!/usr/bin/env python3
"""Common-transcript converter for antigravity agents (invoked by common_transcript.sh).

Reads the raw antigravity transcript (``logs/antigravity_transcript/events.jsonl``,
produced by stream_transcript.sh with each event augmented to carry
``_mngr_conv_id``) and appends semantically important events in the
agent-agnostic common format to ``events/antigravity/common_transcript/events.jsonl``.

It emits:
  USER_EXPLICIT/USER_INPUT     -> user_message  (agy's clean typed text)
  MODEL/PLANNER_RESPONSE       -> assistant_message  (tool_calls attached)
  MODEL/CODE_ACTION            -> tool_result  (paired with the most recent
                                    PLANNER_RESPONSE tool_call in the conversation)
  everything else              -> dropped (bookkeeping / forward-compat)

Tool-call ids are synthetic ("<conv_id>-<step_index>-tc<idx>") since agy's
transcript carries no id on tool_calls. Event ids are deterministic, so
re-processing the same input never produces duplicates (dedup against the set of
event_ids already in the output file).

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


def _extract_user_text(content: JsonValue) -> str | None:
    """Return the user's typed text from a USER_INPUT record.

    agy's SQLite store (the decode_agy_transcript.py source) records the clean typed text
    directly in ``CortexStepUserInput.query``, so ``content`` is already the user's message --
    we only strip surrounding whitespace. A non-string content is a real schema break, so we
    drop the event.
    """
    if not isinstance(content, str):
        return None
    return content.strip()


def _short_value(value: JsonValue) -> str:
    """Render an arbitrary JSON value as a short string for an input preview."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def _tool_call_id(conv_id: str, step_index: JsonValue, idx: int) -> str:
    return f"{conv_id}-{step_index}-tc{idx}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


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


def convert(input_file: str, output_file: str) -> int:
    """Append new common-transcript events from ``input_file`` to ``output_file``; return the count."""
    existing_ids = _load_existing_ids(output_file)
    if not os.path.isfile(input_file):
        return 0

    new_events: list[tuple[str, dict[str, Any]]] = []
    # Track the last assistant tool call we emitted, per conversation, so
    # CODE_ACTION events can be paired with their originating tool call.
    last_tool_call_by_conv: dict[str, dict[str, Any]] = {}

    with open(input_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            conv_id = raw.get("_mngr_conv_id", "")
            if not conv_id:
                continue
            step_index = raw.get("step_index")
            if step_index is None:
                continue
            timestamp = raw.get("created_at", "")
            source = raw.get("source", "")
            type_ = raw.get("type", "")

            if source == "USER_EXPLICIT" and type_ == "USER_INPUT":
                event_id = f"{conv_id}-{step_index}-user"
                if event_id in existing_ids:
                    continue
                text = _extract_user_text(raw.get("content"))
                # _extract_user_text returns the stripped typed text, or None when dropped
                # when content is not a string. Empty results -- a None or otherwise empty
                # content -- are dropped here as they carry no signal.
                if not text:
                    continue
                new_events.append(
                    (
                        timestamp,
                        {
                            "timestamp": timestamp,
                            "type": "user_message",
                            "event_id": event_id,
                            "source": "antigravity/common_transcript",
                            "role": "user",
                            "content": text,
                            "conversation_id": conv_id,
                            "step_index": step_index,
                        },
                    )
                )

            elif source == "MODEL" and type_ == "PLANNER_RESPONSE":
                text = raw.get("content", "")
                raw_tool_calls = raw.get("tool_calls") or []
                tool_calls = []
                for idx, tc in enumerate(raw_tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name", "")
                    args = tc.get("args", {})
                    input_preview = _truncate(_short_value(args), _MAX_INPUT_PREVIEW_LENGTH)
                    call_id = _tool_call_id(conv_id, step_index, idx)
                    tool_calls.append(
                        {
                            "tool_call_id": call_id,
                            "tool_name": name,
                            "input_preview": input_preview,
                        }
                    )

                text_str = text if isinstance(text, str) else ""
                # agy's native format records the text and the tool_calls separately with no
                # information about where each call sat relative to the text, so we can only
                # synthesize a best-effort order (text, then the calls) -> parts_ordered=False.
                parts: list[dict[str, Any]] = []
                if text_str:
                    parts.append({"type": "text", "content": text_str})
                for tc in tool_calls:
                    parts.append({"type": "tool_call", **tc})

                event_id = f"{conv_id}-{step_index}-assistant"
                if event_id not in existing_ids:
                    new_events.append(
                        (
                            timestamp,
                            {
                                "timestamp": timestamp,
                                "type": "assistant_message",
                                "event_id": event_id,
                                "source": "antigravity/common_transcript",
                                "role": "assistant",
                                "model": None,
                                "text": text_str,
                                "tool_calls": tool_calls,
                                "parts": parts,
                                "parts_ordered": False,
                                "finish_reason": None,
                                "usage": None,
                                "conversation_id": conv_id,
                                "step_index": step_index,
                            },
                        )
                    )
                if tool_calls:
                    last_tool_call_by_conv[conv_id] = tool_calls[-1]

            elif source == "MODEL" and type_ == "CODE_ACTION":
                pending = last_tool_call_by_conv.pop(conv_id, None)
                if pending is None:
                    continue
                event_id = f"{conv_id}-{step_index}-tool_result"
                if event_id in existing_ids:
                    continue
                # A non-string content (JSON null, or a list/dict) carries no usable
                # output and would crash _truncate (it calls len()/slices, assuming a
                # str), so drop rather than emit an empty tool_result.
                content = raw.get("content")
                if not isinstance(content, str):
                    continue
                output = _truncate(content, _MAX_OUTPUT_LENGTH)
                new_events.append(
                    (
                        timestamp,
                        {
                            "timestamp": timestamp,
                            "type": "tool_result",
                            "event_id": event_id,
                            "source": "antigravity/common_transcript",
                            "tool_call_id": pending["tool_call_id"],
                            "tool_name": pending["tool_name"],
                            "output": output,
                            "is_error": raw.get("status", "DONE") != "DONE",
                            "conversation_id": conv_id,
                            "step_index": step_index,
                        },
                    )
                )

            else:
                # Bookkeeping (SYSTEM/CONVERSATION_HISTORY) and any future agy
                # source/type combination we don't recognize: dropped best-effort.
                continue

    if not new_events:
        return 0

    new_events.sort(key=lambda x: x[0])
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for _, event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    return len(new_events)


if __name__ == "__main__":
    print(convert(os.environ["_INPUT_FILE"], os.environ["_OUTPUT_FILE"]))
