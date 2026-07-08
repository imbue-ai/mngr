"""Test helpers for building synthetic agy conversation ``.db`` fixtures.

agy stores each conversation as a protobuf SQLite ``.db`` whose ``steps.step_payload`` is a
serialized ``gemini_coder.Step`` (see ``decode_agy_transcript.py`` and ``regenerating_protobuf_schema.md``).
These helpers are the inverse of the decoder's wire-walk: they encode minimal ``Step`` blobs
and write a ``steps`` table, so tests can exercise decoding/streaming without a live agy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# CortexStepStatus / CortexStepSource values used by the builders.
STATUS_DONE = 3
STATUS_GENERATING = 8
SOURCE_MODEL = 2
SOURCE_USER_EXPLICIT = 4
SOURCE_SYSTEM = 5


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _len_field(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _metadata(source: int, seconds: int) -> bytes:
    # CortexStepMetadata: created_at (f1, a Timestamp whose f1 is seconds) and source (f3).
    created_at = _varint_field(1, seconds)
    return _len_field(1, created_at) + _varint_field(3, source)


def step_blob(
    step_type: int,
    status: int,
    *,
    source: int = 0,
    seconds: int = 0,
    content_field: int | None = None,
    content: bytes = b"",
) -> bytes:
    """Encode a ``gemini_coder.Step`` with type/status, optional metadata, and one content sub-message."""
    blob = _varint_field(1, step_type) + _varint_field(4, status)
    if source or seconds:
        blob += _len_field(5, _metadata(source, seconds))
    if content_field is not None:
        blob += _len_field(content_field, content)
    return blob


def user_step(query: str, *, status: int = STATUS_DONE, seconds: int = 0) -> bytes:
    """A USER_INPUT step (type 14) carrying ``query`` in ``CortexStepUserInput.query`` (f19.f1)."""
    inner = _len_field(1, query.encode())
    return step_blob(14, status, source=SOURCE_USER_EXPLICIT, seconds=seconds, content_field=19, content=inner)


def assistant_step(
    response: str,
    *,
    thinking: str = "",
    tool_calls: tuple[tuple[str, str], ...] = (),
    status: int = STATUS_DONE,
    seconds: int = 0,
) -> bytes:
    """A PLANNER_RESPONSE step (type 15).

    ``response`` is f20.f1, ``thinking`` f20.f3, and each ``(name, args)`` in ``tool_calls``
    is a repeated ChatToolCall (f20.f7) with name (f2) and args (f3).
    """
    inner = _len_field(1, response.encode())
    if thinking:
        inner += _len_field(3, thinking.encode())
    for name, args in tool_calls:
        call = _len_field(2, name.encode()) + _len_field(3, args.encode())
        inner += _len_field(7, call)
    return step_blob(15, status, source=SOURCE_MODEL, seconds=seconds, content_field=20, content=inner)


def error_step(text: str, *, status: int = STATUS_DONE, seconds: int = 0) -> bytes:
    """An ERROR_MESSAGE step (type 17) carrying ``text`` as the user-facing error.

    The text lands in ``CortexStepErrorMessage.error`` (f24.f3, a CortexErrorDetails) ->
    ``user_error_message`` (f1).
    """
    details = _len_field(1, text.encode())
    inner = _len_field(3, details)
    return step_blob(17, status, source=SOURCE_SYSTEM, seconds=seconds, content_field=24, content=inner)


def make_conversation_db(path: Path, rows: list[tuple[int, int, int, bytes]]) -> None:
    """Create a minimal agy ``steps`` table at ``path`` from ``(idx, step_type, status, payload)`` rows."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE steps (idx integer, step_type integer, status integer, step_payload blob, PRIMARY KEY (idx))"
        )
        connection.executemany("INSERT INTO steps (idx, step_type, status, step_payload) VALUES (?, ?, ?, ?)", rows)
        connection.commit()
    finally:
        connection.close()
