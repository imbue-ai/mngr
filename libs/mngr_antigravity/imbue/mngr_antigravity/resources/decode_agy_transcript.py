"""Decode agy's SQLite conversation store into the raw-transcript record stream.

Since agy 1.0.4 (2026-06-01) the interactive conversation store is a protobuf SQLite
``.db`` per conversation (``$ANTIGRAVITY_APP_DATA_DIR/conversations/<conv_id>.db``); earlier
versions wrote a per-conversation JSONL transcript that ``stream_transcript.sh`` tailed.
This module replaces that source: it reads new ``steps`` rows from each of this agent's
conversation ``.db`` files and emits one JSON record per step -- in the **same shape the old
JSONL had** (``step_index``/``source``/``type``/``status``/``created_at``/``content`` plus
``_mngr_conv_id``) -- so ``common_transcript.sh`` converts them unchanged.

``steps.step_payload`` is a serialized ``gemini_coder.Step`` protobuf. agy publishes no
schema; the field/enum map below was recovered from the binary's embedded descriptors (see
``libs/mngr_antigravity/dev/README.md`` for the recovery process and how to re-verify it
after an agy release). This decoder is a small, dependency-free protobuf wire-walk -- it
does not need the ``protobuf`` library or any shipped descriptors.

Run on the host (the streamer's environment) under ``python3``; ``stream_transcript.sh``
guards that ``python3`` exists before invoking it. One invocation is a single pass over all
of this agent's conversations; the caller loops.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

# --- gemini_coder.Step field numbers (recovered; see dev/README.md) ----------------------
_STEP_TYPE = 1
_STEP_STATUS = 4
_STEP_METADATA = 5
_STEP_CODE_ACTION = 10
_STEP_USER_INPUT = 19
_STEP_PLANNER_RESPONSE = 20
_STEP_ERROR_MESSAGE = 24
# CortexStepMetadata. created_at is a google.protobuf.Timestamp { f1 seconds; f2 nanos }.
_METADATA_CREATED_AT = 1
_METADATA_SOURCE = 3
_TIMESTAMP_SECONDS = 1
# CortexStepUserInput: the typed message lands in query (f1) or user_response (f2).
_USER_INPUT_QUERY = 1
_USER_INPUT_RESPONSE = 2
# CortexStepPlannerResponse
_PLANNER_RESPONSE_TEXT = 1
_PLANNER_THINKING = 3
# CortexStepErrorMessage: f1 carries the surfaced text (best-effort).
_ERROR_MESSAGE_TEXT = 1

# --- enum value -> the unprefixed name agy used in its JSONL records ----------------------
# Only the names ``common_transcript.sh`` keys off need to be exact; others are informational
# (it drops every type it does not recognise). Unknown values fall back to ``STEP_TYPE_<n>``.
_STEP_TYPE_NAMES = {
    5: "CODE_ACTION",
    14: "USER_INPUT",
    15: "PLANNER_RESPONSE",
    17: "ERROR_MESSAGE",
    98: "CONVERSATION_HISTORY",
    101: "SYSTEM_MESSAGE",
}
_STEP_SOURCE_NAMES = {
    2: "MODEL",
    3: "USER_IMPLICIT",
    4: "USER_EXPLICIT",
    5: "SYSTEM",
    6: "SYSTEM_SDK",
}
_STEP_STATUS_NAMES = {
    1: "PENDING",
    2: "RUNNING",
    3: "DONE",
    4: "INVALID",
    5: "CLEARED",
    6: "CANCELED",
    7: "ERROR",
    8: "GENERATING",
    9: "WAITING",
    11: "QUEUED",
    12: "INTERRUPTED",
}
# A step is "settled" -- safe to emit -- once it reaches one of these terminal statuses. While
# a step is still PENDING/RUNNING/GENERATING/WAITING/QUEUED its content is incomplete, so the
# streamer stops at it and re-checks next pass (preserving in-order, no-skip emission).
_TERMINAL_STATUSES = frozenset({3, 4, 5, 6, 7, 12})

# A 64-bit varint is at most 10 bytes; this bounds every decode loop.
_MAX_VARINT_BYTES = 10
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# CortexStepType values whose content this decoder surfaces (others carry no transcript text).
_TYPE_USER_INPUT = 14
_TYPE_PLANNER_RESPONSE = 15
_TYPE_ERROR_MESSAGE = 17

_WIRE_VARINT = 0
_WIRE_64BIT = 1
_WIRE_LEN = 2
_WIRE_32BIT = 5


class _TruncatedError(Exception):
    """A protobuf blob ended mid-field; the step is skipped and retried next pass."""


def _read_varint(blob: bytes, start: int) -> tuple[int, int]:
    value = 0
    shift = 0
    index = start
    for _ in range(_MAX_VARINT_BYTES):
        if index >= len(blob):
            raise _TruncatedError("varint ran past end of blob")
        byte = blob[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
    raise _TruncatedError("varint exceeded 10 bytes")


def _iter_fields(blob: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield ``(field_number, wire_type, value)``; value is an int (varint) or bytes (len)."""
    index = 0
    length = len(blob)
    while index < length:
        tag, index = _read_varint(blob, index)
        field = tag >> 3
        wire = tag & 7
        if wire == _WIRE_VARINT:
            value, index = _read_varint(blob, index)
            yield field, wire, value
        elif wire == _WIRE_LEN:
            size, index = _read_varint(blob, index)
            if index + size > length:
                raise _TruncatedError("length-delimited field ran past end of blob")
            yield field, wire, blob[index : index + size]
            index += size
        elif wire == _WIRE_64BIT:
            yield field, wire, blob[index : index + 8]
            index += 8
        elif wire == _WIRE_32BIT:
            yield field, wire, blob[index : index + 4]
            index += 4
        else:
            return


def _first(blob: bytes, field_number: int) -> object | None:
    for field, _wire, value in _iter_fields(blob):
        if field == field_number:
            return value
    return None


def _first_str(blob: bytes, field_number: int) -> str:
    value = _first(blob, field_number)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return ""


def _first_message(blob: bytes, field_number: int) -> bytes | None:
    value = _first(blob, field_number)
    return bytes(value) if isinstance(value, (bytes, bytearray)) else None


def _first_varint(blob: bytes, field_number: int) -> int | None:
    value = _first(blob, field_number)
    return value if isinstance(value, int) else None


def _iso_timestamp(metadata: bytes | None) -> str:
    """Render ``metadata.created_at`` (a protobuf Timestamp) as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if metadata is None:
        return ""
    created_at = _first_message(metadata, _METADATA_CREATED_AT)
    if created_at is None:
        return ""
    seconds = _first_varint(created_at, _TIMESTAMP_SECONDS)
    if seconds is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def decode_step(conv_id: str, idx: int, step_type: int, status: int, payload: bytes) -> dict[str, object]:
    """Decode one ``steps`` row into an old-JSONL-shaped record for ``common_transcript.sh``.

    Raises :class:`_TruncatedError` if the protobuf is incomplete (mid-write); the caller
    skips the step and retries on the next pass.
    """
    metadata = _first_message(payload, _STEP_METADATA)
    source_value = _first_varint(metadata, _METADATA_SOURCE) if metadata is not None else None
    record: dict[str, object] = {
        "step_index": idx,
        "source": _STEP_SOURCE_NAMES.get(source_value or 0, f"STEP_SOURCE_{source_value}"),
        "type": _STEP_TYPE_NAMES.get(step_type, f"STEP_TYPE_{step_type}"),
        "status": _STEP_STATUS_NAMES.get(status, f"STEP_STATUS_{status}"),
        "created_at": _iso_timestamp(metadata),
        "_mngr_conv_id": conv_id,
    }
    if step_type == _TYPE_USER_INPUT:
        user_input = _first_message(payload, _STEP_USER_INPUT)
        if user_input is not None:
            record["content"] = _first_str(user_input, _USER_INPUT_QUERY) or _first_str(
                user_input, _USER_INPUT_RESPONSE
            )
    elif step_type == _TYPE_PLANNER_RESPONSE:
        planner = _first_message(payload, _STEP_PLANNER_RESPONSE)
        if planner is not None:
            record["content"] = _first_str(planner, _PLANNER_RESPONSE_TEXT)
            thinking = _first_str(planner, _PLANNER_THINKING)
            if thinking:
                record["thinking"] = thinking
    elif step_type == _TYPE_ERROR_MESSAGE:
        error = _first_message(payload, _STEP_ERROR_MESSAGE)
        if error is not None:
            record["content"] = _first_str(error, _ERROR_MESSAGE_TEXT)
    else:
        # Tool/browser/system steps carry no user-facing text in this decoder;
        # common_transcript.sh drops every type it does not recognise.
        pass
    return record


def _conversation_ids(conversation_ids_file: Path) -> list[str]:
    """Return the distinct, validated conversation UUIDs this agent owns (capture-hook file)."""
    if not conversation_ids_file.is_file():
        return []
    seen: list[str] = []
    for line in conversation_ids_file.read_text().splitlines():
        candidate = line.strip()
        if _UUID_RE.match(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


def _read_offset(offset_dir: Path, conv_id: str) -> int:
    offset_file = offset_dir / conv_id
    if not offset_file.is_file():
        return -1
    text = offset_file.read_text().strip()
    return int(text) if text.lstrip("-").isdigit() else -1


def stream_conversation(db_path: Path, conv_id: str, offset: int) -> tuple[list[str], int]:
    """Return ``(json_lines, new_offset)`` for steps after ``offset`` in ``db_path``.

    Stops at the first non-terminal (still-generating) step so emission stays in order and
    nothing partial is written; that step is picked up once it settles. Opens the database
    read-only so it is safe to read while agy is writing.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    lines: list[str] = []
    new_offset = offset
    try:
        rows = connection.execute(
            "SELECT idx, step_type, status, step_payload FROM steps WHERE idx > ? ORDER BY idx",
            (offset,),
        )
        for idx, step_type, status, payload in rows:
            if status not in _TERMINAL_STATUSES:
                break
            try:
                record = decode_step(conv_id, idx, step_type, status, bytes(payload))
            except _TruncatedError:
                # The step's protobuf is still being written (read mid-flush); stop here without
                # advancing the offset so it is re-read in full on the next pass.
                break
            lines.append(json.dumps(record, separators=(",", ":")))
            new_offset = idx
    finally:
        connection.close()
    return lines, new_offset


def run_once(state_dir: Path, app_data_dir: Path) -> int:
    """Do one pass over every conversation this agent owns; return the number of records emitted."""
    conversation_ids_file = state_dir / "antigravity_conversation_ids"
    output_file = state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
    offset_dir = state_dir / "plugin" / "antigravity" / ".transcript_offsets"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    offset_dir.mkdir(parents=True, exist_ok=True)

    emitted = 0
    with output_file.open("a") as sink:
        for conv_id in _conversation_ids(conversation_ids_file):
            db_path = app_data_dir / "conversations" / f"{conv_id}.db"
            if not db_path.is_file():
                continue
            offset = _read_offset(offset_dir, conv_id)
            try:
                lines, new_offset = stream_conversation(db_path, conv_id, offset)
            except sqlite3.Error:
                # A transiently locked or mid-checkpoint db: skip this conversation this pass and
                # retry next pass rather than failing the whole cycle.
                continue
            for line in lines:
                sink.write(line + "\n")
            emitted += len(lines)
            if new_offset != offset:
                (offset_dir / conv_id).write_text(str(new_offset))
    return emitted


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode agy SQLite conversations into raw transcript records.")
    parser.add_argument("--state-dir", type=Path, required=True, help="$MNGR_AGENT_STATE_DIR")
    parser.add_argument("--app-data-dir", type=Path, required=True, help="$ANTIGRAVITY_APP_DATA_DIR")
    args = parser.parse_args()
    run_once(args.state_dir, args.app_data_dir)


if __name__ == "__main__":
    main()
