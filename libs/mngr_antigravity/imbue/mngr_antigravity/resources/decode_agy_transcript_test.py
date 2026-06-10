"""Unit tests for the agy SQLite conversation decoder.

Builds synthetic ``gemini_coder.Step`` protobuf blobs (the inverse of the decoder's
wire-walk) and a minimal ``steps`` table, then exercises decoding, terminal-status gating,
per-conversation offset persistence, and conversation-id scoping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imbue.mngr_antigravity.resources import decode_agy_transcript as dat
from imbue.mngr_antigravity.resources.testing import assistant_step as _assistant_step
from imbue.mngr_antigravity.resources.testing import error_step as _error_step
from imbue.mngr_antigravity.resources.testing import make_conversation_db as _make_db
from imbue.mngr_antigravity.resources.testing import step_blob as _step
from imbue.mngr_antigravity.resources.testing import user_step as _user_step


def _setup(tmp_path: Path, conv_id: str, rows: list[tuple[int, int, int, bytes]]) -> tuple[Path, Path]:
    """Create the agent state dir + app-data dir with a conversation .db and ids file."""
    state_dir = tmp_path / "state"
    app_data_dir = tmp_path / "appdata"
    (app_data_dir / "conversations").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    _make_db(app_data_dir / "conversations" / f"{conv_id}.db", rows)
    (state_dir / "antigravity_conversation_ids").write_text(conv_id + "\n")
    return state_dir, app_data_dir


def _read_events(state_dir: Path) -> list[dict[str, object]]:
    output = state_dir / "logs" / "antigravity_transcript" / "events.jsonl"
    if not output.is_file():
        return []
    return [json.loads(line) for line in output.read_text().splitlines() if line.strip()]


_CONV = "abcdef01-2345-6789-abcd-ef0123456789"


def test_decode_user_input_extracts_clean_query() -> None:
    record = dat.decode_step(_CONV, 0, 14, 3, _user_step("hello there", seconds=1780000000))
    assert record["type"] == "USER_INPUT"
    assert record["source"] == "USER_EXPLICIT"
    assert record["status"] == "DONE"
    assert record["content"] == "hello there"
    assert record["created_at"] == "2026-05-28T20:26:40Z"
    assert record["_mngr_conv_id"] == _CONV


def test_decode_planner_response_includes_thinking() -> None:
    record = dat.decode_step(_CONV, 2, 15, 3, _assistant_step("the answer", thinking="hmm"))
    assert record["type"] == "PLANNER_RESPONSE"
    assert record["source"] == "MODEL"
    assert record["content"] == "the answer"
    assert record["thinking"] == "hmm"


def test_decode_planner_response_extracts_tool_calls() -> None:
    blob = _assistant_step(
        "running it",
        tool_calls=(("run_command", '{"CommandLine":"uv --version"}'), ("read_file", '{"path":"x"}')),
    )
    record = dat.decode_step(_CONV, 3, 15, 3, blob)
    assert record["tool_calls"] == [
        {"name": "run_command", "args": '{"CommandLine":"uv --version"}'},
        {"name": "read_file", "args": '{"path":"x"}'},
    ]


def test_decode_planner_response_without_tool_calls_omits_the_key() -> None:
    record = dat.decode_step(_CONV, 2, 15, 3, _assistant_step("just text"))
    assert "tool_calls" not in record


def test_decode_unknown_type_falls_back_to_numeric_name() -> None:
    record = dat.decode_step(_CONV, 1, 21, 3, _step(21, 3, source=2))
    assert record["type"] == "STEP_TYPE_21"
    assert "content" not in record


def test_run_once_emits_user_and_assistant_turn(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(
        tmp_path,
        _CONV,
        # idx 1 is CONVERSATION_HISTORY (type 98): emitted to raw, dropped by common_transcript.
        [
            (0, 14, 3, _user_step("remember SECRET-42")),
            (1, 98, 3, _step(98, 3, source=5)),
            (2, 15, 3, _assistant_step("ok SECRET-42 noted")),
        ],
    )
    emitted = dat.run_once(state_dir, app_data_dir)
    assert emitted == 3
    events = _read_events(state_dir)
    assert [e["type"] for e in events] == ["USER_INPUT", "CONVERSATION_HISTORY", "PLANNER_RESPONSE"]
    assert events[0]["content"] == "remember SECRET-42"
    assert events[2]["content"] == "ok SECRET-42 noted"


def test_run_once_stops_at_non_terminal_step_then_resumes(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(
        tmp_path,
        _CONV,
        # idx 1 is still GENERATING (status 8): not terminal, so it is not emitted yet.
        [
            (0, 14, 3, _user_step("question")),
            (1, 15, 8, _assistant_step("partial...", status=8)),
        ],
    )
    # Only the terminal user step is emitted on the first pass.
    assert dat.run_once(state_dir, app_data_dir) == 1
    assert [e["type"] for e in _read_events(state_dir)] == ["USER_INPUT"]

    # The assistant step settles to DONE: rewrite the row, and a second pass picks it up once.
    db_path = app_data_dir / "conversations" / f"{_CONV}.db"
    db_path.unlink()
    _make_db(db_path, [(0, 14, 3, _user_step("question")), (1, 15, 3, _assistant_step("complete answer"))])
    assert dat.run_once(state_dir, app_data_dir) == 1
    assert [e["type"] for e in _read_events(state_dir)] == ["USER_INPUT", "PLANNER_RESPONSE"]
    assert _read_events(state_dir)[1]["content"] == "complete answer"


def test_offset_persists_so_second_pass_emits_nothing(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, _user_step("hi"))])
    assert dat.run_once(state_dir, app_data_dir) == 1
    assert dat.run_once(state_dir, app_data_dir) == 0
    assert len(_read_events(state_dir)) == 1


def test_only_conversations_in_ids_file_are_read(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, _user_step("tracked"))])
    other = "ffffffff-1111-2222-3333-444444444444"
    _make_db(app_data_dir / "conversations" / f"{other}.db", [(0, 14, 3, _user_step("foreign"))])
    dat.run_once(state_dir, app_data_dir)
    assert [e["content"] for e in _read_events(state_dir)] == ["tracked"]


def test_malformed_and_duplicate_ids_lines_are_ignored(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, _user_step("hi"))])
    (state_dir / "antigravity_conversation_ids").write_text(f"not-a-uuid\n{_CONV}\n{_CONV}\n")
    assert dat.run_once(state_dir, app_data_dir) == 1


def test_missing_db_is_skipped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    app_data_dir = tmp_path / "appdata"
    (app_data_dir / "conversations").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    (state_dir / "antigravity_conversation_ids").write_text(_CONV + "\n")
    assert dat.run_once(state_dir, app_data_dir) == 0


def test_truncated_payload_is_skipped(tmp_path: Path) -> None:
    full = _user_step("complete")
    # idx 1's blob is truncated mid-field (still being written).
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, full), (1, 14, 3, full[:-3])])
    # Step 0 emits; step 1 is truncated so the pass stops there without advancing past it.
    assert dat.run_once(state_dir, app_data_dir) == 1
    assert [e["content"] for e in _read_events(state_dir)] == ["complete"]


# --- coverage of error/edge branches and the CLI entrypoint ------------------------------


def test_decode_error_message_extracts_text() -> None:
    record = dat.decode_step(_CONV, 3, 17, 3, _error_step("server is busy"))
    assert record["type"] == "ERROR_MESSAGE"
    assert record["content"] == "server is busy"


def test_iso_timestamp_handles_missing_and_partial_metadata() -> None:
    # No metadata at all.
    assert dat._iso_timestamp(None) == ""
    # Metadata present but no created_at (only source f3 = 0x18 0x05).
    assert dat._iso_timestamp(b"\x18\x05") == ""
    # created_at present (f1) but empty, so no seconds (f1.f1).
    assert dat._iso_timestamp(b"\x0a\x00") == ""


def test_iter_fields_handles_fixed_width_and_stops_on_invalid_wire() -> None:
    # field 1 as fixed64 (wire 1, tag 0x09) then fixed32 (wire 5, tag 0x0d).
    fields = list(dat._iter_fields(b"\x09" + b"\x00" * 8 + b"\x0d" + b"\x01" * 4))
    assert [(f, w) for f, w, _v in fields] == [(1, 1), (1, 5)]
    # An unsupported wire type (3) terminates iteration without raising.
    assert list(dat._iter_fields(b"\x0b")) == []


def test_read_varint_rejects_truncated_and_overlong_varints() -> None:
    # A single continuation byte with no following byte: the varint runs past the end.
    with pytest.raises(dat._TruncatedError):
        list(dat._iter_fields(b"\x80"))
    # Eleven continuation bytes: the varint never terminates within 10 bytes.
    with pytest.raises(dat._TruncatedError):
        list(dat._iter_fields(b"\x80" * 11))


def test_run_once_skips_a_corrupt_db_but_keeps_others(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, _user_step("ok"))])
    corrupt = "ffffffff-0000-0000-0000-000000000000"
    (app_data_dir / "conversations" / f"{corrupt}.db").write_bytes(b"this is not a sqlite database")
    (state_dir / "antigravity_conversation_ids").write_text(f"{_CONV}\n{corrupt}\n")
    # The corrupt db raises sqlite3.Error and is skipped; the good conversation still emits.
    assert dat.run_once(state_dir, app_data_dir) == 1
    assert [event["content"] for event in _read_events(state_dir)] == ["ok"]


def test_run_once_with_no_conversation_ids_file_emits_nothing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    app_data_dir = tmp_path / "appdata"
    (app_data_dir / "conversations").mkdir(parents=True)
    state_dir.mkdir(parents=True)
    assert dat.run_once(state_dir, app_data_dir) == 0


def test_main_runs_one_pass(tmp_path: Path) -> None:
    state_dir, app_data_dir = _setup(tmp_path, _CONV, [(0, 14, 3, _user_step("via main"))])
    dat.main(["--state-dir", str(state_dir), "--app-data-dir", str(app_data_dir)])
    assert [event["content"] for event in _read_events(state_dir)] == ["via main"]
