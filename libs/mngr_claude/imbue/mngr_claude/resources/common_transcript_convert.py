#!/usr/bin/env python3
"""Common-transcript converter for claude agents (invoked by common_transcript.sh).

Reads the raw Claude transcript (``logs/claude_transcript/events.jsonl``,
produced by stream_transcript.sh) and appends the semantically important events
in the agent-agnostic ATIF-shaped stream format (see
``specs/atif-transcript-alignment/spec.md`` and the record schema in
``imbue.mngr.agents.common_transcript_records``) to
``events/claude/common_transcript/events.jsonl``. Noise (progress events,
file-history snapshots, system bookkeeping) is dropped.

Each output line is a ``header``, ``step`` or ``observation`` record. Fidelity is
full: complete tool ``arguments`` objects, untruncated tool output, and thinking
blocks as ``reasoning_content``. Display truncation belongs to the reader.

Dedup is ID-based: each output ``event_id`` is derived from the source event's
uuid, so re-processing the same input never produces duplicate output.

Invoked as ``python3 common_transcript_convert.py`` with the input/output paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` environment variables that
common_transcript.sh sets. Malformed or null lines are dropped silently; only an
uncaught exception writes to stderr, which the shell reports as a convert error
(the count of appended lines is printed to stdout for common_transcript.sh to
capture). A standalone module rather than inline in the shell script, so the
logic is lintable, type-checked, and unit-testable directly rather than only
through a subprocess.

Every pass materializes the whole input and the whole output rather than reading
from a saved offset. That is a deliberate trade: the grouping below needs a
lane's full history to decide which inferences are finished, dedup needs the ids
already emitted, and a full parse of a session-sized file costs milliseconds.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Union

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

_EMITTER = "claude/common_transcript"

# The ATIF revision these records follow; must match PINNED_ATIF_SCHEMA_VERSION in
# imbue.mngr.agents.common_transcript_records (restated because this script runs on
# the agent's host with only the stdlib and cannot import mngr).
_SCHEMA_VERSION = "ATIF-v1.7"


# The stream header is written once, on the first pass that appends anything; its
# event_id doubles as its dedup key.
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


# Set to "1" by common_transcript.sh's --single-pass path (a turn-end flush, where
# the last inference is known to be complete). See _group_assistant_records for why
# the trailing group is otherwise held back.
_TRAILING_GROUP_ENV_VAR = "_MNGR_EMIT_TRAILING_ASSISTANT_GROUP"

# Multimodal capture is out of scope; images become this placeholder wherever text
# is extracted (spec: "Fidelity rules").
_IMAGE_PLACEHOLDER = "[image omitted]"

# A slash command the user types (``/foo bar``) is not recorded verbatim: Claude
# Code expands it into plumbing records -- the expansion tags (led by
# <command-name> for built-ins, <command-message> for custom commands), the
# local execution output (<local-command-stdout>), and an isMeta caveat wrapper
# (<local-command-caveat>). None of these is a conversation turn, so the
# converter drops them entirely (the chat UI's session parser filters the same
# markup). The check anchors on the leading tag, so a genuine turn that merely
# quotes the markup mid-text is kept.
_COMMAND_PLUMBING_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
)


def _is_command_plumbing(text: str) -> bool:
    return text.lstrip().startswith(_COMMAND_PLUMBING_PREFIXES)


def _is_sidechain(record: Mapping[str, Any]) -> bool:
    """Whether a raw record belongs to the sidechain lane (a native Task subagent).

    Claude writes a native subagent's records into the *same* session file as the
    main thread, interleaved with it and marked ``isSidechain: true``. The two
    conversations are independent, so they are grouped independently (see
    ``_grouped_inferences``) and the provenance rides on the emitted records so a
    consumer can carve the subagent back out.
    """
    return bool(record.get("isSidechain"))


def _block_text(block: Mapping[str, Any]) -> str:
    """The transcript-visible text of one content block ("" when it carries none)."""
    block_type = block.get("type", "")
    if block_type == "text":
        text = block.get("text", "")
        return text if isinstance(text, str) else ""
    if block_type == "image":
        return _IMAGE_PLACEHOLDER
    return ""


def _extract_text_content(content: JsonValue) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [_block_text(block) for block in content if isinstance(block, dict)]
    return "\n".join(part for part in parts if part)


def _extract_tool_result_text(raw_result: JsonValue) -> str:
    """Normalize a tool_result block's content into a single output string."""
    if isinstance(raw_result, str):
        return raw_result
    if not isinstance(raw_result, list):
        return str(raw_result)
    parts: list[str] = []
    for item in raw_result:
        if isinstance(item, dict):
            parts.append(_block_text(item))
        elif isinstance(item, str):
            parts.append(item)
        else:
            # Unknown item shape: no text to extract.
            continue
    return "\n".join(parts)


def _has_tool_results_only(content: JsonValue) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type", "")
            if block_type not in ("tool_result",):
                return False
        elif isinstance(block, str):
            return False
        else:
            # Unknown block shape (not a dict or str): ignore it, as the original
            # converter did -- it neither confirms nor denies tool-results-only.
            continue
    return True


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _token_count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) else 0


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
            except (json.JSONDecodeError, KeyError, TypeError):
                # Not a record carrying an id: malformed JSON, an object without the
                # key, or a valid-JSON scalar (which a string key cannot index).
                continue
    return ids


class _AssistantGroup:
    """The assistant lines of one LLM inference, keyed by their shared ``message.id``.

    Claude fans a single API response out over several consecutive JSONL lines --
    one per content block (thinking, text, each tool_use) -- all carrying the same
    ``message.id`` and the same usage. ATIF wants one step per inference, so the
    lines are regrouped here. Lines of one group are *not* always adjacent: with
    parallel tool calls, each tool_use line is followed by its own tool_result line
    before the next tool_use line of the same response, so tool-result-only user
    records do not break a group (anything else does).
    """

    def __init__(self, message_id: str, first_uuid: str, first_timestamp: str, is_sidechain: bool) -> None:
        self.message_id = message_id
        self.event_id = _make_event_id(first_uuid, "assistant")
        self.timestamp = first_timestamp
        self.is_sidechain = is_sidechain
        self.records: list[Mapping[str, Any]] = []


def _group_assistant_records(
    records: Sequence[Mapping[str, Any]], is_sidechain: bool
) -> tuple[list[_AssistantGroup], _AssistantGroup | None]:
    """Group one lane's records into inferences; return them and the open trailing one.

    ``records`` must be a single lane (see ``_grouped_inferences``): a record of one
    lane says nothing about whether the other lane's inference is finished.

    A group is *closed* once the lane proves no more of its lines can arrive: a
    later assistant record with a different ``message.id``, or a user record that is
    not purely tool results. The lane's last group has no such proof -- claude is
    still appending to it -- and callers must hold it back, because the output is
    deduped by ``event_id`` and a group emitted half-written would stay half-written
    forever.
    """
    groups: list[_AssistantGroup] = []
    open_group: _AssistantGroup | None = None
    for record in records:
        message = record["message"]
        if record["type"] == "assistant":
            message_id = message.get("id") or ""
            # A record with no message id can never be shown to belong with another,
            # so it is always its own group (keyed by its uuid, which is unique).
            is_same_group = open_group is not None and message_id != "" and open_group.message_id == message_id
            if not is_same_group:
                open_group = _AssistantGroup(message_id, record["uuid"], record["timestamp"], is_sidechain)
                groups.append(open_group)
            open_group.records.append(record)
        elif not _has_tool_results_only(message.get("content")):
            open_group = None
        else:
            # A tool-result-only user record is the other half of an in-flight
            # inference, not a new turn: it leaves the open group open.
            continue
    return groups, open_group


def _grouped_inferences(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[_AssistantGroup], list[_AssistantGroup]]:
    """Group every inference in the input; return all groups and the open trailing ones.

    A native subagent's records are interleaved with the main thread's in the same
    file, so the records are partitioned into lanes (main thread, sidechain) and
    each lane is grouped on its own -- otherwise a subagent's prompt would close the
    main thread's in-flight inference and split it into two steps with double-counted
    usage. Each lane holds back its own trailing group: one conversation being still
    in flight says nothing about the other.
    """
    groups: list[_AssistantGroup] = []
    open_groups: list[_AssistantGroup] = []
    for is_sidechain in (False, True):
        lane_records = [record for record in records if _is_sidechain(record) == is_sidechain]
        lane_groups, lane_open_group = _group_assistant_records(lane_records, is_sidechain)
        groups.extend(lane_groups)
        if lane_open_group is not None:
            open_groups.append(lane_open_group)
    return groups, open_groups


def _build_agent_step(group: _AssistantGroup) -> dict[str, Any] | None:
    """Build the ATIF agent step for one inference, or None when it has no content."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    model = ""
    stop_reason = None
    usage: Mapping[str, Any] = {}
    for record in group.records:
        message = record["message"]
        if not model and isinstance(message.get("model"), str):
            model = message["model"]
        if stop_reason is None and isinstance(message.get("stop_reason"), str):
            stop_reason = message["stop_reason"]
        # Every line of a group repeats the same usage for the one API response;
        # read it once so the inference's tokens are not counted per line.
        if not usage and isinstance(message.get("usage"), dict):
            usage = message["usage"]
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "thinking":
                thinking = block.get("thinking", "") or block.get("text", "")
                if isinstance(thinking, str) and thinking:
                    thinking_parts.append(thinking)
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "tool_call_id": block.get("id", ""),
                        "function_name": block.get("name", ""),
                        "arguments": _tool_arguments(block.get("input")),
                    }
                )
            else:
                text = _block_text(block)
                if text:
                    text_parts.append(text)

    if not text_parts and not thinking_parts and not tool_calls:
        # Nothing the model said, thought, or did (e.g. a redacted-thinking-only
        # response): emitting it would render as an empty turn.
        return None

    step: dict[str, Any] = {
        "type": "step",
        "event_id": group.event_id,
        "emitter": _EMITTER,
        "timestamp": group.timestamp,
        "source": "agent",
        "message": "\n".join(text_parts),
        # A group is the lines of one API response, so exactly one inference.
        "llm_call_count": 1,
    }
    if thinking_parts:
        step["reasoning_content"] = "\n\n".join(thinking_parts)
    if tool_calls:
        step["tool_calls"] = tool_calls
    if model:
        step["model_name"] = model
    if usage:
        step["metrics"] = _build_metrics(usage)
    extra: dict[str, Any] = {}
    if stop_reason:
        extra["finish_reason"] = stop_reason
    if group.message_id:
        extra["message_id"] = group.message_id
    if group.is_sidechain:
        extra["is_sidechain"] = True
    if extra:
        step["extra"] = extra
    return step


def _tool_arguments(raw_input: JsonValue) -> dict[str, Any]:
    """The complete ATIF ``arguments`` object for a tool_use block.

    Claude records the parsed input, so this is normally passed through whole; a
    non-object input is wrapped rather than dropped (spec: "Fidelity rules").
    """
    if isinstance(raw_input, dict):
        return raw_input
    if raw_input is None:
        return {}
    return {"_raw": raw_input if isinstance(raw_input, str) else json.dumps(raw_input, separators=(",", ":"))}


def _build_metrics(usage: Mapping[str, Any]) -> dict[str, Any]:
    """Map claude's usage onto ATIF metric names.

    ATIF's ``prompt_tokens`` is *all* input tokens including cached ones, which
    claude reports as three separate counters.
    """
    cache_read_tokens = _token_count(usage, "cache_read_input_tokens")
    cache_creation_tokens = _token_count(usage, "cache_creation_input_tokens")
    metrics: dict[str, Any] = {
        "prompt_tokens": _token_count(usage, "input_tokens") + cache_read_tokens + cache_creation_tokens,
        "completion_tokens": _token_count(usage, "output_tokens"),
        "cached_tokens": cache_read_tokens,
    }
    if "cache_creation_input_tokens" in usage:
        # Cache *writes* have no ATIF field of their own.
        metrics["extra"] = {"cache_creation_input_tokens": cache_creation_tokens}
    return metrics


def _build_user_records(
    record: Mapping[str, Any], tool_name_by_call_id: Mapping[str, str], skipped_call_ids: frozenset[str]
) -> list[dict[str, Any]]:
    """Build the step and observation records one native user line contributes."""
    uuid = record["uuid"]
    timestamp = record["timestamp"]
    message = record["message"]
    content = message.get("content")
    is_sidechain = _is_sidechain(record)
    events: list[dict[str, Any]] = []

    if not _has_tool_results_only(content):
        text = _extract_text_content(content)
        if record.get("isCompactSummary"):
            events.append(_build_compaction_step(uuid, timestamp, text, is_sidechain))
        elif _is_command_plumbing(text):
            # Slash-command plumbing (expansion tags, local stdout, the isMeta
            # caveat wrapper) is not a conversation turn: emit nothing for it, on
            # both the meta and plain paths.
            pass
        elif not text:
            # An empty user message carries no signal.
            pass
        else:
            # Framework-injected content (stop hook output, local-command caveats)
            # is a system step rather than a user turn: nobody typed it.
            is_meta = bool(record.get("isMeta"))
            step: dict[str, Any] = {
                "type": "step",
                "event_id": _make_event_id(uuid, "meta" if is_meta else "user"),
                "emitter": _EMITTER,
                "timestamp": timestamp,
                "source": "system" if is_meta else "user",
                "message": text,
            }
            if is_sidechain:
                step["extra"] = {"is_sidechain": True}
            events.append(step)

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id", "")
            if not call_id or call_id in skipped_call_ids:
                # A result whose call sits in the held-back trailing group waits for
                # it, so the stream never shows an output before the call that made it.
                continue
            result_extra: dict[str, Any] = {
                "is_error": bool(block.get("is_error", False)),
                # Neither has an ATIF field of its own; the reader uses both.
                "tool_name": tool_name_by_call_id.get(call_id, "unknown"),
            }
            if is_sidechain:
                result_extra["is_sidechain"] = True
            events.append(
                {
                    "type": "observation",
                    "event_id": _make_event_id(uuid, f"tool_result-{call_id}"),
                    "emitter": _EMITTER,
                    "timestamp": timestamp,
                    "results": [
                        {
                            "source_call_id": call_id,
                            "content": _extract_tool_result_text(block.get("content", "")),
                            "extra": result_extra,
                        }
                    ],
                }
            )
    return events


def _build_compaction_step(uuid: str, timestamp: str, summary: str, is_sidechain: bool) -> dict[str, Any]:
    """Build the system step for a context-compaction boundary (ATIF v1.7 convention)."""
    extra: dict[str, Any] = {"context_management": {"type": "compaction", "boundary": "replace"}}
    if is_sidechain:
        extra["is_sidechain"] = True
    step: dict[str, Any] = {
        "type": "step",
        "event_id": _make_event_id(uuid, "compact"),
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": "system",
        "message": "Context compaction performed",
        "extra": extra,
    }
    if summary:
        # A system step already has its result at emission time, so the summary rides
        # inline rather than as a separate observation record.
        step["observation"] = {"results": [{"content": summary}]}
    return step


def _tool_call_ids(group: _AssistantGroup) -> frozenset[str]:
    """The ids of every tool call one inference issued."""
    return frozenset(
        block["id"]
        for record in group.records
        for block in record["message"].get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
    )


def _tool_names_by_call_id(records: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Map every tool_use id in the input to its tool name.

    Built from the *whole* input, before any dedup skip: a tool_result converted in
    a later pass than its (already-emitted) call still needs the name, or it would
    be labeled "unknown" forever.
    """
    names: dict[str, str] = {}
    for record in records:
        if record["type"] != "assistant":
            continue
        for block in record["message"].get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") and block.get("name"):
                names[block["id"]] = block["name"]
    return names


def _read_conversation_records(input_file: str) -> list[Mapping[str, Any]]:
    """Parse the raw transcript into its usable user/assistant records, in file order."""
    records: list[Mapping[str, Any]] = []
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
            if raw.get("type") not in ("user", "assistant"):
                # Noise: progress, file-history-snapshot, system, result, etc.
                continue
            if not raw.get("uuid") or not raw.get("timestamp"):
                continue
            if not isinstance(raw.get("message"), dict):
                # A null/missing message carries no usable content -- drop the line
                # rather than emit an empty event or crash.
                continue
            records.append(raw)
    return records


def convert(input_file: str, output_file: str, is_input_complete: bool = False, agent_id: str = "") -> int:
    """Append new common-transcript records from ``input_file`` to ``output_file``; return the count.

    Set ``is_input_complete`` only when the input is known to be complete (a turn-end
    ``--single-pass`` flush); otherwise each lane's last assistant inference is held
    back until a later line proves it finished (see ``_group_assistant_records``).

    ``agent_id`` seeds the stream's header event id; the production entrypoint
    passes the agent state directory's basename.

    Even that flush reads a file stream_transcript.sh appends to without taking the
    convert lock, so a pass can in theory materialize the input mid-append and see a
    half-written final line. That line is dropped as malformed and read complete on
    the next pass, and the inference it belongs to is only emitted once a later line
    closes it -- the accepted risk is a moment of lag, never a truncated record.
    """
    existing_ids = _load_existing_ids(output_file)

    if not os.path.isfile(input_file):
        return 0

    records = _read_conversation_records(input_file)
    tool_name_by_call_id = _tool_names_by_call_id(records)

    groups, open_groups = _grouped_inferences(records)
    skipped_groups = [] if is_input_complete else open_groups
    skipped_call_ids = frozenset(call_id for group in skipped_groups for call_id in _tool_call_ids(group))
    step_by_first_uuid: dict[str, dict[str, Any]] = {}
    for group in groups:
        if group in skipped_groups:
            continue
        step = _build_agent_step(group)
        if step is not None:
            step_by_first_uuid[group.records[0]["uuid"]] = step

    new_events: list[dict[str, Any]] = []
    for record in records:
        if record["type"] == "assistant":
            # Each group is emitted once, at its first line.
            step = step_by_first_uuid.get(record["uuid"])
            if step is not None and step["event_id"] not in existing_ids:
                new_events.append(step)
        else:
            for event in _build_user_records(record, tool_name_by_call_id, skipped_call_ids):
                if event["event_id"] not in existing_ids:
                    new_events.append(event)

    if not new_events:
        return 0

    new_events.sort(key=lambda event: event["timestamp"])
    if _header_event_id(agent_id) not in existing_ids:
        # The header is the first line of every stream, written on first append.
        new_events.insert(
            0,
            {
                "type": "header",
                "event_id": _header_event_id(agent_id),
                "emitter": _EMITTER,
                "schema_version": _SCHEMA_VERSION,
            },
        )

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for event in new_events:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")

    return len(new_events)


if __name__ == "__main__":
    _state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    _agent_id = os.path.basename(os.path.normpath(_state_dir)) if _state_dir else ""
    print(
        convert(
            os.environ["_INPUT_FILE"],
            os.environ["_OUTPUT_FILE"],
            is_input_complete=os.environ.get(_TRAILING_GROUP_ENV_VAR) == "1",
            agent_id=_agent_id,
        )
    )
