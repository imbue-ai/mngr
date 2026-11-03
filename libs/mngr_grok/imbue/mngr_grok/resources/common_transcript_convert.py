#!/usr/bin/env python3
"""Common-transcript converter for grok agents, run by ``common_transcript.sh``.

Reads the raw transcript ``logs/grok_transcript/events.jsonl`` (each line tags one
native ``updates.jsonl`` record with its ``session_id``, ``parent_session_id`` and
byte ``offset``) and appends ATIF-shaped records (``header`` / ``step`` /
``observation``, see ``imbue/mngr/agents/common_transcript_records.py``) to
``events/grok/common_transcript/events.jsonl``.

Each session is its own lane; a child session's records carry
``extra.is_sidechain`` and ``extra.parent_session_id`` in the same stream. Per lane:

  user_message_chunk        -> user step (``_meta.hideFromScrollback`` wake prompts are
                               skipped; the send-now ``<user_query>`` wrapper is unwrapped)
  agent_message_chunk,      -> one agent step per inference: message text, reasoning and
  agent_thought_chunk,         tool calls accumulate until a tool result, a stop-hook
  tool_call                    continuation (``hook_annotation``), the session's end or
                               the turn's end closes the step
  tool_call_update          -> observation record when its status is completed or failed
  subagent_spawned          -> ``extra`` link on the spawning tool call and its result
  turn_completed            -> ``extra.finish_reason`` and ``metrics`` on the step it
                               closes (an empty agent step when none is open)
  anything else             -> ignored

An agent step's ``model_name`` is the lane's last user-chunk ``_meta.modelId``; without one,
a step a turn closes takes the one model the turn's ``usage.modelUsage`` names, if exactly one.

Records are appended in input order, never sorted. Event ids derive from (session id,
native byte offset), so reconverting the same input never duplicates a record; a step
still open at the end of the input is emitted only when ``_MNGR_EMIT_OPEN_STEPS=1``.

Invoked as ``python3 common_transcript_convert.py`` with ``_INPUT_FILE`` and
``_OUTPUT_FILE`` set; warnings go to stderr. Standard library only, compatible with
Python 3.9.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from typing import Optional

_EMITTER: Final = "grok/common_transcript"
# Must match PINNED_ATIF_SCHEMA_VERSION in imbue/mngr/agents/common_transcript_records.py.
_SCHEMA_VERSION: Final = "ATIF-v1.7"
_SEND_NOW_WRAPPER: Final = re.compile(
    r"\AThe user sent a message while you were working:\s*<user_query>\n?(.*?)\n?</user_query>", re.DOTALL
)
_TERMINAL_TOOL_CALL_STATUSES: Final = frozenset({"completed", "failed"})
_SPAWN_SUBAGENT_TOOL_NAME: Final = "spawn_subagent"
_METRIC_FIELDS: Final = (
    ("prompt_tokens", "inputTokens"),
    ("completion_tokens", "outputTokens"),
    ("cached_tokens", "cachedReadTokens"),
)
_METRIC_EXTRA_FIELDS: Final = (
    ("total_tokens", "totalTokens"),
    ("reasoning_tokens", "reasoningTokens"),
    ("cache_creation_tokens", "cacheCreationTokens"),
    ("model_calls", "modelCalls"),
)
_LOGGER: Final = logging.getLogger("grok_common_transcript_convert")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _as_count(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _header_event_id(agent_id: str) -> str:
    """The stream's header id, hashed from the agent id and emitter so every agent's header is distinct."""
    digest = hashlib.sha256("{}:{}".format(agent_id, _EMITTER).encode("utf-8", "replace")).hexdigest()[:32]
    return "header-" + digest


def build_header_record(agent_id: str) -> dict[str, Any]:
    return {
        "type": "header",
        "event_id": _header_event_id(agent_id),
        "emitter": _EMITTER,
        "schema_version": _SCHEMA_VERSION,
    }


def record_timestamp(record: Mapping[str, Any]) -> str:
    """The native record's time as ISO 8601 UTC with milliseconds, e.g. ``2026-10-20T01:14:10.029Z``."""
    timestamp_ms = _as_count(_as_dict(_as_dict(record.get("params")).get("_meta")).get("agentTimestampMs"))
    timestamp_s = _as_count(record.get("timestamp"))
    if timestamp_ms is not None:
        epoch_ms = timestamp_ms
    elif timestamp_s is not None:
        epoch_ms = timestamp_s * 1000
    else:
        epoch_ms = int(time.time() * 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_ms // 1000)) + ".{:03d}Z".format(epoch_ms % 1000)


def unwrap_user_text(text: str) -> str:
    """Return the user's own words from grok's send-now wrapper, or the text unchanged."""
    match = _SEND_NOW_WRAPPER.match(text)
    return match.group(1) if match is not None else text


def build_metrics(usage: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for atif_name, grok_name in _METRIC_FIELDS:
        count = _as_count(usage.get(grok_name))
        if count is not None:
            metrics[atif_name] = count
    extra: dict[str, Any] = {}
    for extra_name, grok_name in _METRIC_EXTRA_FIELDS:
        count = _as_count(usage.get(grok_name))
        if count is not None:
            extra[extra_name] = count
    if usage.get("usageIsIncomplete") is True:
        extra["usage_is_incomplete"] = True
    if extra:
        metrics["extra"] = extra
    return metrics


def build_tool_arguments(raw_input: Any) -> dict[str, Any]:
    if isinstance(raw_input, dict):
        return raw_input
    if raw_input is None:
        return {}
    return {"_raw": raw_input if isinstance(raw_input, str) else json.dumps(raw_input, separators=(",", ":"))}


def _tool_result_text(update: Mapping[str, Any]) -> Optional[str]:
    content = update.get("content")
    if not isinstance(content, list):
        return None
    texts = [
        _as_text(_as_dict(_as_dict(item).get("content")).get("text"))
        for item in content
        if _as_dict(item).get("type") == "content"
    ]
    present_texts = [text for text in texts if text is not None]
    return "\n".join(present_texts) if present_texts else None


def _new_lane(session_id: str, parent_session_id: Optional[str]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "open_step": None,
        "model_id": None,
        "tool_call_by_id": {},
    }


def _lane_extra(lane: Mapping[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {"session_id": lane["session_id"]}
    if lane["parent_session_id"] is not None:
        extra["parent_session_id"] = lane["parent_session_id"]
        extra["is_sidechain"] = True
    return extra


def _open_step(kind: str, lane: Mapping[str, Any], offset: int, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "event_id": "grok-{}-{}-{}".format(lane["session_id"], offset, kind),
        "timestamp": record_timestamp(record),
        "texts": [],
        "reasoning_texts": [],
        "tool_call_ids": [],
        "prompt_index": None,
        "turn_end": None,
    }


def _build_tool_call(lane: Mapping[str, Any], tool_call_id: str) -> dict[str, Any]:
    tool_call = lane["tool_call_by_id"][tool_call_id]
    built: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "function_name": tool_call["function_name"],
        "arguments": tool_call["arguments"],
    }
    if tool_call["subagent"] is not None:
        built["extra"] = dict(tool_call["subagent"])
    return built


def _build_agent_step(lane: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "step",
        "event_id": step["event_id"],
        "emitter": _EMITTER,
        "timestamp": step["timestamp"],
        "source": "agent",
        "message": "".join(step["texts"]),
    }
    if step["reasoning_texts"]:
        record["reasoning_content"] = "".join(step["reasoning_texts"])
    if step["tool_call_ids"]:
        record["tool_calls"] = [_build_tool_call(lane, tool_call_id) for tool_call_id in step["tool_call_ids"]]
    extra = _lane_extra(lane)
    turn_end = step["turn_end"]
    usage = _as_dict(turn_end.get("usage")) if turn_end is not None else {}
    model_usage = _as_dict(usage.get("modelUsage"))
    model_name = lane["model_id"] or (next(iter(model_usage)) if len(model_usage) == 1 else None)
    if model_name is not None:
        record["model_name"] = model_name
    if turn_end is not None:
        stop_reason = _as_text(turn_end.get("stop_reason"))
        if stop_reason is not None:
            extra["finish_reason"] = stop_reason
        prompt_id = _as_text(turn_end.get("prompt_id"))
        if prompt_id is not None:
            extra["prompt_id"] = prompt_id
        agent_result = _as_text(turn_end.get("agent_result"))
        if agent_result is not None:
            extra["agent_result"] = agent_result
        metrics = build_metrics(usage)
        if metrics:
            record["metrics"] = metrics
    record["extra"] = extra
    return record


def _build_user_step(lane: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": step["event_id"],
        "emitter": _EMITTER,
        "timestamp": step["timestamp"],
        "source": "user",
        "message": "".join(step["texts"]),
        "extra": _lane_extra(lane),
    }


def _close_open_step(lane: dict[str, Any], records: list[dict[str, Any]]) -> None:
    step = lane["open_step"]
    if step is None:
        return
    lane["open_step"] = None
    if step["kind"] == "agent":
        records.append(_build_agent_step(lane, step))
    else:
        records.append(_build_user_step(lane, step))


def _ensure_open_step(
    kind: str, lane: dict[str, Any], offset: int, record: Mapping[str, Any], records: list[dict[str, Any]]
) -> dict[str, Any]:
    step = lane["open_step"]
    if step is not None and step["kind"] == kind:
        return step
    _close_open_step(lane, records)
    lane["open_step"] = _open_step(kind, lane, offset, record)
    return lane["open_step"]


def _on_user_message_chunk(
    lane: dict[str, Any],
    update: Mapping[str, Any],
    offset: int,
    record: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> None:
    chunk_meta = _as_dict(update.get("_meta"))
    model_id = _as_text(chunk_meta.get("modelId"))
    if model_id is not None:
        lane["model_id"] = model_id
    if chunk_meta.get("hideFromScrollback") is True:
        _close_open_step(lane, records)
        return
    text = _as_text(_as_dict(update.get("content")).get("text"))
    if not text:
        return
    open_step = lane["open_step"]
    prompt_index = chunk_meta.get("promptIndex")
    if open_step is None or open_step["kind"] != "user" or open_step["prompt_index"] != prompt_index:
        _close_open_step(lane, records)
        lane["open_step"] = _open_step("user", lane, offset, record)
        lane["open_step"]["prompt_index"] = prompt_index
    lane["open_step"]["texts"].append(unwrap_user_text(text))


def _on_tool_call(
    lane: dict[str, Any],
    update: Mapping[str, Any],
    offset: int,
    record: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> None:
    tool_call_id = _as_text(update.get("toolCallId"))
    if tool_call_id is None or tool_call_id in lane["tool_call_by_id"]:
        return
    tool_meta = _as_dict(_as_dict(update.get("_meta")).get("x.ai/tool"))
    function_name = _as_text(tool_meta.get("name")) or _as_text(update.get("title")) or ""
    raw_input = update.get("rawInput")
    step = _ensure_open_step("agent", lane, offset, record, records)
    step["tool_call_ids"].append(tool_call_id)
    lane["tool_call_by_id"][tool_call_id] = {
        "function_name": function_name,
        "arguments": build_tool_arguments(raw_input),
        "description": _as_text(_as_dict(raw_input).get("description")),
        "subagent": None,
        "has_result": False,
    }


def _on_tool_call_update(
    lane: dict[str, Any],
    update: Mapping[str, Any],
    offset: int,
    record: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> None:
    status = _as_text(update.get("status"))
    tool_call = lane["tool_call_by_id"].get(_as_text(update.get("toolCallId")))
    if status not in _TERMINAL_TOOL_CALL_STATUSES or tool_call is None or tool_call["has_result"]:
        return
    tool_call["has_result"] = True
    open_step = lane["open_step"]
    if open_step is not None and update["toolCallId"] in open_step["tool_call_ids"]:
        _close_open_step(lane, records)
    result: dict[str, Any] = {"source_call_id": update["toolCallId"]}
    text = _tool_result_text(update)
    if text is not None:
        result["content"] = text
    result_extra = {**_lane_extra(lane), "is_error": status == "failed", "tool_name": tool_call["function_name"]}
    if tool_call["subagent"] is not None:
        result_extra.update(tool_call["subagent"])
    result["extra"] = result_extra
    records.append(
        {
            "type": "observation",
            "event_id": "grok-{}-{}-observation".format(lane["session_id"], offset),
            "emitter": _EMITTER,
            "timestamp": record_timestamp(record),
            "results": [result],
        }
    )


def _on_subagent_spawned(lane: dict[str, Any], update: Mapping[str, Any]) -> None:
    child_session_id = _as_text(update.get("child_session_id")) or _as_text(update.get("subagent_id"))
    if child_session_id is None:
        return
    candidates = [
        tool_call
        for tool_call in lane["tool_call_by_id"].values()
        if tool_call["function_name"] == _SPAWN_SUBAGENT_TOOL_NAME
        and tool_call["subagent"] is None
        and not tool_call["has_result"]
    ]
    description = _as_text(update.get("description"))
    matching = [tool_call for tool_call in candidates if tool_call["description"] == description] or candidates
    if not matching:
        return
    link: dict[str, Any] = {"subagent_session_id": child_session_id}
    subagent_type = _as_text(update.get("subagent_type"))
    if subagent_type is not None:
        link["subagent_type"] = subagent_type
    matching[0]["subagent"] = link


def _on_turn_completed(
    lane: dict[str, Any],
    update: Mapping[str, Any],
    offset: int,
    record: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> None:
    step = _ensure_open_step("agent", lane, offset, record, records)
    step["turn_end"] = update
    _close_open_step(lane, records)
    lane["tool_call_by_id"] = {}


def _apply_raw_entry(
    lanes: dict[str, dict[str, Any]], entry: Mapping[str, Any], records: list[dict[str, Any]]
) -> None:
    """Fold one raw-transcript line into its session's lane, appending any records it completes."""
    session_id = _as_text(entry.get("session_id"))
    offset = _as_count(entry.get("offset"))
    record = entry.get("record")
    if session_id is None or offset is None or not isinstance(record, dict):
        return
    if session_id not in lanes:
        lanes[session_id] = _new_lane(session_id, _as_text(entry.get("parent_session_id")))
    lane = lanes[session_id]
    update = _as_dict(_as_dict(record.get("params")).get("update"))
    kind = update.get("sessionUpdate")
    if kind == "user_message_chunk":
        _on_user_message_chunk(lane, update, offset, record, records)
    elif kind in ("agent_message_chunk", "agent_thought_chunk"):
        text = _as_text(_as_dict(update.get("content")).get("text"))
        if text:
            step = _ensure_open_step("agent", lane, offset, record, records)
            step["texts" if kind == "agent_message_chunk" else "reasoning_texts"].append(text)
    elif kind == "tool_call":
        _on_tool_call(lane, update, offset, record, records)
    elif kind == "tool_call_update":
        _on_tool_call_update(lane, update, offset, record, records)
    elif kind == "subagent_spawned":
        _on_subagent_spawned(lane, update)
    elif kind == "turn_completed":
        _on_turn_completed(lane, update, offset, record, records)
    elif kind == "hook_annotation" or (kind == "hook_execution" and update.get("event_name") == "session_end"):
        _close_open_step(lane, records)
    else:
        pass


def build_common_records(entries: Iterable[Mapping[str, Any]], is_emitting_open_steps: bool) -> list[dict[str, Any]]:
    """Convert raw-transcript entries to common records, in emission order (no header, no dedup)."""
    lanes: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for entry in entries:
        _apply_raw_entry(lanes, entry, records)
    if is_emitting_open_steps:
        for lane in lanes.values():
            _close_open_step(lane, records)
    return records


def _read_raw_entries(input_file: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with open(input_file, encoding="utf-8", errors="replace") as raw_file:
        for line_index, line in enumerate(raw_file):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                _LOGGER.warning("Skipped unparseable raw transcript line %d: %s", line_index + 1, error)
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _load_existing_event_ids(output_file: str) -> set[str]:
    event_ids: set[str] = set()
    if not os.path.isfile(output_file):
        return event_ids
    with open(output_file, encoding="utf-8", errors="replace") as output:
        for line_index, line in enumerate(output):
            if not line.strip():
                continue
            try:
                event_id = _as_dict(json.loads(line)).get("event_id")
            except json.JSONDecodeError as error:
                _LOGGER.warning("Ignored unparseable common transcript line %d: %s", line_index + 1, error)
                continue
            if isinstance(event_id, str):
                event_ids.add(event_id)
    return event_ids


def _select_new_records(
    records: Sequence[Mapping[str, Any]], existing_event_ids: set[str], agent_id: str
) -> list[Mapping[str, Any]]:
    new_records: list[Mapping[str, Any]] = []
    seen_event_ids = set(existing_event_ids)
    for record in records:
        if record["event_id"] not in seen_event_ids:
            seen_event_ids.add(record["event_id"])
            new_records.append(record)
    if new_records and _header_event_id(agent_id) not in existing_event_ids:
        new_records.insert(0, build_header_record(agent_id))
    return new_records


def _append_on_a_fresh_line(output_file: str, data: bytes) -> None:
    """Append ``data``, first ending a torn last line so the appended lines never join it."""
    with open(output_file, "ab+") as output:
        output.seek(0, os.SEEK_END)
        if output.tell() > 0:
            output.seek(-1, os.SEEK_END)
            if output.read(1) != b"\n":
                output.write(b"\n")
        output.write(data)


def convert(input_file: str, output_file: str, is_emitting_open_steps: bool, agent_id: str) -> int:
    """Append the not-yet-emitted common records for ``input_file`` to ``output_file``; return how many."""
    if not os.path.isfile(input_file):
        return 0
    records = build_common_records(_read_raw_entries(input_file), is_emitting_open_steps)
    new_records = _select_new_records(records, _load_existing_event_ids(output_file), agent_id)
    if not new_records:
        return 0
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    lines = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in new_records)
    _append_on_a_fresh_line(output_file, lines.encode("utf-8"))
    return len(new_records)


def convert_from_environment(environ: Mapping[str, str]) -> int:
    """Run ``convert`` on the files, flag and agent state directory ``common_transcript.sh`` passes in ``environ``."""
    state_dir = environ.get("MNGR_AGENT_STATE_DIR", "")
    agent_id = os.path.basename(os.path.normpath(state_dir)) if state_dir else ""
    return convert(
        environ["_INPUT_FILE"],
        environ["_OUTPUT_FILE"],
        is_emitting_open_steps=environ.get("_MNGR_EMIT_OPEN_STEPS") == "1",
        agent_id=agent_id,
    )


def main() -> None:
    logging.basicConfig(format="%(message)s", level=logging.WARNING)
    convert_from_environment(os.environ)


if __name__ == "__main__":
    main()
