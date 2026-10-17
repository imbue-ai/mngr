"""Test helpers for the antigravity resource scripts.

agy stores each conversation as a protobuf SQLite ``.db`` whose ``steps.step_payload`` is a
serialized ``gemini_coder.Step`` (see ``decode_agy_transcript.py`` and ``regenerating_protobuf_schema.md``).
The ``*_step`` / ``make_conversation_db`` helpers are the inverse of the decoder's wire-walk: they
encode minimal ``Step`` blobs and write a ``steps`` table, so tests can exercise decoding/streaming
without a live agy.

The ``*_event`` builders mint decoded raw-transcript events as plain dicts -- the shape
``stream_transcript.sh`` appends and ``common_transcript_convert.py`` reads. They are shared by the
converter's unit tests and the shell-level tests, which JSON-encode them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# CortexStepStatus / CortexStepSource values used by the builders.
STATUS_DONE = 3
STATUS_GENERATING = 8
SOURCE_MODEL = 2
SOURCE_USER_EXPLICIT = 4
SOURCE_SYSTEM = 5

_DEFAULT_TRANSCRIPT_TIMESTAMP = "2026-05-21T07:00:00Z"


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


def transcript_event(
    *,
    conv_id: str,
    step_index: int,
    source: str,
    type_: str,
    timestamp: str = _DEFAULT_TRANSCRIPT_TIMESTAMP,
    # Deliberately untyped: tests seed malformed raw-stream shapes through these too.
    content: Any = None,
    tool_calls: list[Any] | None = None,
    thinking: str | None = None,
    status: str = "DONE",
) -> dict[str, Any]:
    """One raw-transcript event, including the ``_mngr_conv_id`` the streamer adds."""
    body: dict[str, Any] = {
        "step_index": step_index,
        "source": source,
        "type": type_,
        "status": status,
        "created_at": timestamp,
        "_mngr_conv_id": conv_id,
    }
    if content is not None:
        body["content"] = content
    if tool_calls is not None:
        body["tool_calls"] = tool_calls
    if thinking is not None:
        body["thinking"] = thinking
    return body


def user_input_event(conv_id: str, step_index: int, prompt_text: Any, **kwargs: Any) -> dict[str, Any]:
    """USER_EXPLICIT/USER_INPUT carrying the clean typed text agy's SQLite store records."""
    return transcript_event(
        conv_id=conv_id,
        step_index=step_index,
        source="USER_EXPLICIT",
        type_="USER_INPUT",
        content=prompt_text,
        **kwargs,
    )


def planner_response_event(
    conv_id: str,
    step_index: int,
    text: Any = "",
    tool_calls: list[Any] | None = None,
    thinking: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return transcript_event(
        conv_id=conv_id,
        step_index=step_index,
        source="MODEL",
        type_="PLANNER_RESPONSE",
        content=text,
        tool_calls=tool_calls,
        thinking=thinking,
        **kwargs,
    )


def code_action_event(conv_id: str, step_index: int, content: Any, status: str = "DONE") -> dict[str, Any]:
    return transcript_event(
        conv_id=conv_id,
        step_index=step_index,
        source="MODEL",
        type_="CODE_ACTION",
        content=content,
        status=status,
    )


def conversation_history_event(conv_id: str, step_index: int) -> dict[str, Any]:
    """SYSTEM/CONVERSATION_HISTORY bookkeeping, which the converter must drop."""
    return transcript_event(conv_id=conv_id, step_index=step_index, source="SYSTEM", type_="CONVERSATION_HISTORY")
