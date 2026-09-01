#!/usr/bin/env python3
"""Common-transcript converter for antigravity agents (invoked by common_transcript.sh).

Reads the raw antigravity transcript (``logs/antigravity_transcript/events.jsonl``,
produced by stream_transcript.sh with each event augmented to carry
``_mngr_conv_id``) and appends semantically important events to
``events/antigravity/common_transcript/events.jsonl`` as ATIF-shaped stream
records (``header`` / ``step`` / ``observation``; see
``specs/atif-transcript-alignment/spec.md`` and the canonical schema in
``imbue/mngr/agents/common_transcript_records.py``).

It emits:
  USER_EXPLICIT/USER_INPUT     -> user step  (agy's clean typed text)
  MODEL/PLANNER_RESPONSE       -> agent step (tool_calls attached; the planner's
                                    thinking becomes reasoning_content)
  MODEL/CODE_ACTION            -> observation record (paired with the most recent
                                    PLANNER_RESPONSE tool_call in the conversation)
  everything else              -> dropped (bookkeeping / forward-compat)

Planner thinking is *not* a record of its own: ``decode_agy_transcript.py`` reads it
from ``CortexStepPlannerResponse``'s thinking field and hangs it on the same
PLANNER_RESPONSE record as a ``thinking`` key, so it lands directly on that step's
``reasoning_content`` with no cross-record merging to do.

Records are appended in input-stream order, never sorted: the doc-builder treats
append order as authoritative, and a record whose ``created_at`` the decoder
degraded takes conversion time as its timestamp, which would sort a tool output
away from the step that called it.

Tool-call ids are synthetic ("<conv_id>-<step_index>-tc<idx>") since agy's
transcript carries no id on tool_calls. Event ids are deterministic, so
re-processing the same input never produces duplicates (dedup against the set of
event_ids already in the output file, the ``header`` line included).

Nothing is truncated: ``arguments`` is the complete decoded args object (agy
serializes them as a JSON string; anything that does not parse to an object rides
whole under ``_raw``) and observation ``content`` is the full output text. agy's
own per-conversation annotations (``conversation_id``, ``step_index``) live under
the ATIF ``extra`` objects, since every other field on a record is an ATIF field.

Invoked as ``python3 common_transcript_convert.py`` with the input/output paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` environment variables that
common_transcript.sh sets. Malformed or null lines are dropped silently; only an
uncaught exception writes to stderr, which the shell reports as a convert error
(the count of appended records is printed to stdout for common_transcript.sh to
capture). Split out of
the shell script (it used to be an inline ``python3`` heredoc) so the logic is
lintable, type-checked, and unit-testable directly rather than only through a
subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from typing import Union

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

_EMITTER = "antigravity/common_transcript"
# The ATIF revision these records follow; must match PINNED_ATIF_SCHEMA_VERSION in
# imbue/mngr/agents/common_transcript_records.py.
_SCHEMA_VERSION = "ATIF-v1.7"


def _parse_arguments(args: JsonValue) -> dict[str, Any]:
    """Return the complete ATIF ``arguments`` object for a decoded tool call.

    agy's ``ChatToolCall.args`` is a JSON string. Whatever does not parse to a JSON
    object rides whole under ``_raw`` so nothing is lost.
    """
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        # An absent or empty native payload means "no arguments", not a raw empty string.
        if not args.strip():
            return {}
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return {"_raw": args}
        return parsed if isinstance(parsed, dict) else {"_raw": args}
    return {"_raw": json.dumps(args, separators=(",", ":"))}


def _tool_call_id(conv_id: str, step_index: JsonValue, idx: int) -> str:
    return f"{conv_id}-{step_index}-tc{idx}"


def _iso_timestamp(value: JsonValue) -> str:
    """Return the record's ISO 8601 timestamp, falling back to conversion time.

    ATIF step and observation records require a timestamp, and the decoder degrades
    a corrupt or absent ``created_at`` to "". Conversion time is the closest
    approximation available on the host, and the doc-builder orders by stream
    position rather than by timestamp.
    """
    if isinstance(value, str) and value:
        return value
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _header_event_id(agent_id: str) -> str:
    """A per-stream header id, hashed from the agent id and emitter.

    A fixed "header" id repeats identically for every agent on every host, so
    analytics' fleet-wide event-id dedupe collapses all header rows to one --
    destroying the per-agent (emitter, schema_version) mix. The agent id is a
    UUID4-based value, so distinct agents never collide; a migrated agent keeps
    its id on the new host, which is the right grain for its one logical stream.
    """
    digest = hashlib.sha256(f"{agent_id}:{_EMITTER}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"header-{digest}"


def _header_record(agent_id: str) -> dict[str, Any]:
    return {
        "type": "header",
        "event_id": _header_event_id(agent_id),
        "emitter": _EMITTER,
        "schema_version": _SCHEMA_VERSION,
    }


def _annotations(conv_id: str, step_index: JsonValue) -> dict[str, Any]:
    """agy's per-conversation annotations, as they ride under an ATIF ``extra`` object."""
    return {"conversation_id": conv_id, "step_index": step_index}


def _step_record(event_id: str, timestamp: str, source: str, message: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": source,
        "message": message,
        "extra": extra,
    }


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


def convert(input_file: str, output_file: str, agent_id: str = "") -> int:
    """Append new common-transcript records from ``input_file`` to ``output_file``; return the count.

    ``agent_id`` seeds the stream's header event id; the production entrypoint
    passes the agent state directory's basename.
    """
    existing_ids = _load_existing_ids(output_file)
    if not os.path.isfile(input_file):
        return 0

    # Records in input-stream order: the doc-builder treats append order as
    # authoritative, so an observation must never precede its own call's step.
    records: list[dict[str, Any]] = []
    # Track the last tool call we emitted, per conversation, so CODE_ACTION events
    # can be paired with their originating call. Each value is (tool_call_id, tool_name).
    last_tool_call_by_conv: dict[str, tuple[str, str]] = {}

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
            timestamp = _iso_timestamp(raw.get("created_at"))
            source = raw.get("source", "")
            type_ = raw.get("type", "")

            if source == "USER_EXPLICIT" and type_ == "USER_INPUT":
                event_id = f"{conv_id}-{step_index}-user"
                if event_id in existing_ids:
                    continue
                # agy's SQLite store records the clean typed text directly in
                # CortexStepUserInput.query, so content is already the user's message.
                # A non-string content is a real schema break and an empty one carries
                # no signal, so both are dropped.
                content = raw.get("content")
                text = content.strip() if isinstance(content, str) else ""
                if not text:
                    continue
                records.append(_step_record(event_id, timestamp, "user", text, _annotations(conv_id, step_index)))

            elif source == "MODEL" and type_ == "PLANNER_RESPONSE":
                text = raw.get("content", "")
                raw_tool_calls = raw.get("tool_calls") or []
                tool_calls: list[dict[str, Any]] = []
                # The last call of the turn, as (tool_call_id, tool_name): agy emits one
                # CODE_ACTION per planner response, and it belongs to that call.
                last_call: tuple[str, str] | None = None
                for idx, tc in enumerate(raw_tool_calls):
                    if not isinstance(tc, dict):
                        continue
                    name = tc.get("name")
                    tool_name = name if isinstance(name, str) else ""
                    call_id = _tool_call_id(conv_id, step_index, idx)
                    tool_calls.append(
                        {
                            "tool_call_id": call_id,
                            "function_name": tool_name,
                            "arguments": _parse_arguments(tc.get("args")),
                        }
                    )
                    last_call = (call_id, tool_name)

                event_id = f"{conv_id}-{step_index}-assistant"
                if event_id not in existing_ids:
                    step = _step_record(
                        event_id,
                        timestamp,
                        "agent",
                        text if isinstance(text, str) else "",
                        _annotations(conv_id, step_index),
                    )
                    thinking = raw.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        step["reasoning_content"] = thinking
                    if tool_calls:
                        step["tool_calls"] = tool_calls
                    records.append(step)
                if last_call is not None:
                    last_tool_call_by_conv[conv_id] = last_call

            elif source == "MODEL" and type_ == "CODE_ACTION":
                pending = last_tool_call_by_conv.pop(conv_id, None)
                # ATIF requires source_call_id on every streamed result, and agy's
                # transcript carries no id on the action itself, so a CODE_ACTION with
                # no preceding tool call has nothing to attach to.
                if pending is None:
                    continue
                event_id = f"{conv_id}-{step_index}-tool_result"
                if event_id in existing_ids:
                    continue
                # A non-string content (JSON null, or a list/dict) carries no usable
                # output text, so drop it rather than emit an empty result.
                content = raw.get("content")
                if not isinstance(content, str):
                    continue
                call_id, tool_name = pending
                records.append(
                    {
                        "type": "observation",
                        "event_id": event_id,
                        "emitter": _EMITTER,
                        "timestamp": timestamp,
                        "results": [
                            {
                                "source_call_id": call_id,
                                "content": content,
                                "extra": {
                                    "is_error": raw.get("status", "DONE") != "DONE",
                                    "tool_name": tool_name,
                                    **_annotations(conv_id, step_index),
                                },
                            }
                        ],
                    }
                )

            else:
                # Bookkeeping (SYSTEM/CONVERSATION_HISTORY) and any future agy
                # source/type combination we don't recognize: dropped best-effort.
                continue

    if not records:
        return 0

    # The header is the first line of the stream, written on the first append and
    # deduped by its event_id like every other record.
    if _header_event_id(agent_id) not in existing_ids:
        records.insert(0, _header_record(agent_id))

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    return len(records)


if __name__ == "__main__":
    _state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    _agent_id = os.path.basename(os.path.normpath(_state_dir)) if _state_dir else ""
    print(convert(os.environ["_INPUT_FILE"], os.environ["_OUTPUT_FILE"], agent_id=_agent_id))
