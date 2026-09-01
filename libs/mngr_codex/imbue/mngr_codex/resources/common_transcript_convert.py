#!/usr/bin/env python3
"""Common-transcript converter for codex agents (invoked by common_transcript.sh).

Reads the raw codex rollout stream (``logs/codex_transcript/events.jsonl``,
produced verbatim by stream_transcript.sh) and appends the semantically
important rollout items to ``events/codex/common_transcript/events.jsonl`` as
ATIF-shaped stream records (``header`` / ``step`` / ``observation``; see
``specs/atif-transcript-alignment/spec.md`` and the canonical schema in
``imbue/mngr/agents/common_transcript_records.py``).

codex rollout wire shape (verified live against codex 0.64.0 and the patched
codex 0.146.0 build):
  {"timestamp":"<ISO8601>","type":<t>,"payload":<p>}
Each rollout item under type "response_item" maps to exactly one record:
  payload.type=="message", role=="user"      -> user step (or a system step, for
                                    the instruction injections described below)
  payload.type=="message", role=="assistant" -> agent step (message only;
                                    dropped when it carries no text)
  payload.type=="reasoning"                  -> agent step carrying
                                    reasoning_content (dropped when the item
                                    exposes no extractable text)
  payload.type=="function_call"              -> agent step with one tool_call
                                    (arguments parsed from payload.arguments);
                                    the tool name is remembered by payload.call_id
  payload.type=="function_call_output"       -> observation record, keyed by call_id
  payload.type=="custom_tool_call"           -> agent step with one tool_call; the
                                    0.146 unified exec tool emits these
                                    (payload.input, not .arguments)
  payload.type=="custom_tool_call_output"    -> observation record, keyed by call_id

User-role messages that are instruction injections rather than genuine user
turns -- the AGENTS.md context blob ("# AGENTS.md instructions for <dir>" with
an "<INSTRUCTIONS>" envelope) and codex's own "<user_instructions>" /
"<environment_context>" initial-context items -- become *system* steps carrying
their full text, so the session-configured instructions survive without being
mistaken for user turns.

codex models a tool invocation as its own rollout item, separate from the
assistant's text (a distinct ``message`` item), so a call is emitted as its own
agent step with an empty ``message`` -- matching ATIF's one-step-per-inference
convention. The tool_call_id is codex's own ``call_id``, so the doc-builder pairs
each observation result back to its step by that native id.

Nothing is truncated: ``arguments`` is the complete parsed object (an invocation
payload that is not a JSON object rides whole under ``_raw``) and observation
``content`` is the full stringified output.

Event ids are synthesized by hashing the line's own timestamp and content (plus
the item kind), so re-processing the same input never produces duplicates and ids
never repeat across agents or hosts; the converter also dedupes against the set of
event_ids already in the output file (the ``header`` line included).

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

_EMITTER = "codex/common_transcript"
# The ATIF revision these records follow; must match PINNED_ATIF_SCHEMA_VERSION in
# imbue/mngr/agents/common_transcript_records.py.
_SCHEMA_VERSION = "ATIF-v1.7"

# The tool name recorded on an observation whose call was never seen (a rollout
# tailed from mid-turn). The result is still emitted -- the doc-builder warns on an
# unmatched source_call_id rather than the output being lost here.
_UNKNOWN_TOOL_NAME = "unknown"

# codex wraps its own initial-context items in these envelopes and carries them
# as user-role messages; they are session-configured instructions, not user turns.
_CONTEXT_INJECTION_PREFIXES = ("<user_instructions>", "<environment_context>")
# The AGENTS.md context injection (seen live on codex 0.146.0): a user-role
# message opening with this header, with the file body in an <INSTRUCTIONS> envelope.
_AGENTS_MD_INJECTION_PREFIX = "# AGENTS.md instructions for "
_AGENTS_MD_INJECTION_ENVELOPE = "<INSTRUCTIONS>"

# Fields of a ``reasoning`` rollout item that can carry plain text: ``summary[]``
# holds ``summary_text`` items, and ``content[]`` holds ``reasoning_text`` items on
# builds that expose it. The always-present ``encrypted_content`` is opaque.
_REASONING_TEXT_FIELDS = ("summary", "content")


def _is_injected_instructions(text: str) -> bool:
    """True for instruction-injection messages that are system steps, not user turns."""
    stripped = text.lstrip()
    if stripped.startswith(_AGENTS_MD_INJECTION_PREFIX) and _AGENTS_MD_INJECTION_ENVELOPE in stripped:
        return True
    return stripped.startswith(_CONTEXT_INJECTION_PREFIXES)


def _iso_timestamp(value: JsonValue) -> str:
    """Return the record's ISO 8601 timestamp, falling back to conversion time.

    ATIF step and observation records require a timestamp, but a truncated or
    malformed rollout line can lack one. Conversion time is the closest
    approximation available on the host, and the doc-builder orders by stream
    position rather than by timestamp.
    """
    if isinstance(value, str) and value:
        return value
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def _join_reasoning_text(payload: dict[str, Any]) -> str:
    """Extract the plain reasoning text of a ``reasoning`` rollout item, best-effort.

    Blocks are joined with a blank line, per the spec's reasoning_content rule.
    Returns "" when the item exposes nothing beyond its encrypted payload.
    """
    blocks = []
    for field_name in _REASONING_TEXT_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                blocks.append(text)
    return "\n\n".join(blocks)


def _parse_arguments(invocation: JsonValue) -> dict[str, Any]:
    """Return the complete ATIF ``arguments`` object for a native invocation payload.

    codex serializes a call's arguments as a JSON string (``function_call.arguments``)
    or as a free-form script string (``custom_tool_call.input``, which is JavaScript,
    not JSON). Whatever does not parse to a JSON object rides whole under ``_raw`` so
    nothing is lost.
    """
    if isinstance(invocation, dict):
        return invocation
    if isinstance(invocation, str):
        # An absent or empty native payload means "no arguments", not a raw empty string.
        if not invocation.strip():
            return {}
        try:
            parsed = json.loads(invocation)
        except json.JSONDecodeError:
            return {"_raw": invocation}
        return parsed if isinstance(parsed, dict) else {"_raw": invocation}
    return {"_raw": json.dumps(invocation, separators=(",", ":"))}


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


def _step_record(event_id: str, timestamp: str, source: str, message: str) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": source,
        "message": message,
    }


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
    # Tool names of calls awaiting their output, keyed by codex's native call_id
    # (which is also the ATIF tool_call_id).
    pending_tool_name_by_call_id: dict[str, str] = {}

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

            raw_timestamp = raw.get("timestamp")
            timestamp = _iso_timestamp(raw_timestamp)
            # Hash the line's own timestamp, never _iso_timestamp's conversion-time
            # fallback: a wall-clock fallback would mint a fresh id on every run and
            # re-append the record each time.
            id_timestamp = raw_timestamp if isinstance(raw_timestamp, str) else ""
            payload_type = payload.get("type")

            if payload_type == "message" and payload.get("role") == "user":
                text = _join_content_text(payload.get("content"), "input_text")
                # An empty user message carries no signal -> drop it.
                if not text:
                    continue
                # Instruction injections ride in as user-role messages; they are
                # session configuration, so they become system steps instead.
                kind = "system" if _is_injected_instructions(text) else "user"
                event_id = _make_event_id(id_timestamp, text, kind)
                if _is_already_converted(existing_ids, event_id, line_index, kind):
                    continue
                records.append(_step_record(event_id, timestamp, kind, text))

            elif payload_type == "message" and payload.get("role") == "assistant":
                text = _join_content_text(payload.get("content"), "output_text")
                # An assistant message with no text carries no signal (codex models a
                # tool invocation as its own item, so there is nothing else on it) ->
                # drop it, exactly as an empty user message is dropped.
                if not text:
                    continue
                event_id = _make_event_id(id_timestamp, text, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    continue
                records.append(_step_record(event_id, timestamp, "agent", text))

            elif payload_type == "reasoning":
                reasoning = _join_reasoning_text(payload)
                # An item whose only payload is encrypted_content exposes no reasoning
                # text, so there is nothing to record.
                if not reasoning:
                    continue
                event_id = _make_event_id(id_timestamp, reasoning, "reasoning")
                if _is_already_converted(existing_ids, event_id, line_index, "reasoning"):
                    continue
                reasoning_step = _step_record(event_id, timestamp, "agent", "")
                reasoning_step["reasoning_content"] = reasoning
                records.append(reasoning_step)

            elif payload_type in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id")
                # Without codex's native call id there is no tool_call_id to pair a
                # result to, so the call (and its later output) is dropped.
                if not isinstance(call_id, str) or not call_id:
                    continue
                name = payload.get("name")
                tool_name = name if isinstance(name, str) else ""
                pending_tool_name_by_call_id[call_id] = tool_name
                # The 0.146 unified exec tool carries its invocation under "input";
                # every other call kind carries it under "arguments".
                invocation = (
                    payload.get("input", "") if payload_type == "custom_tool_call" else payload.get("arguments", "")
                )
                invocation_text = (
                    invocation if isinstance(invocation, str) else json.dumps(invocation, separators=(",", ":"))
                )
                event_id = _make_event_id(id_timestamp, invocation_text, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    continue
                call_step = _step_record(event_id, timestamp, "agent", "")
                call_step["tool_calls"] = [
                    {
                        "tool_call_id": call_id,
                        "function_name": tool_name,
                        "arguments": _parse_arguments(invocation),
                    }
                ]
                records.append(call_step)

            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                # ATIF requires source_call_id on every streamed result; without the
                # native call id there is nothing to attach the result to.
                if not isinstance(call_id, str) or not call_id:
                    continue
                content = _stringify_output(payload.get("output", ""))
                event_id = _make_event_id(id_timestamp, content, "tool_result")
                if _is_already_converted(existing_ids, event_id, line_index, "tool_result"):
                    continue
                # An output whose call was never seen is still emitted, under the
                # "unknown" tool name; the doc-builder warns on the unmatched id.
                tool_name = pending_tool_name_by_call_id.pop(call_id, _UNKNOWN_TOOL_NAME)
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
                                "extra": {"is_error": False, "tool_name": tool_name},
                            }
                        ],
                    }
                )

            else:
                # Other payload types (web_search_call, ...) and non-user/assistant
                # message roles (codex's own developer-role instruction items) are
                # bookkeeping, not conversation content.
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
