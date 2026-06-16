"""Tests for the antigravity stream_transcript.sh supervisor.

Since agy 1.0.4 the streamer is a thin python3-guarded loop around
``decode_agy_transcript.py``, which reads agy's per-conversation protobuf SQLite ``.db``.
These tests run the bash wrapper end-to-end (``--single-pass``) against synthetic ``.db``
fixtures (see ``resources/testing.py``); the decode logic itself is covered in
``decode_agy_transcript_test.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_antigravity.resources.testing import assistant_step
from imbue.mngr_antigravity.resources.testing import make_conversation_db
from imbue.mngr_antigravity.resources.testing import user_step

_HERE = Path(__file__).parent
_SCRIPT_PATH = _HERE / "stream_transcript.sh"
_DECODER_PATH = _HERE / "decode_agy_transcript.py"
_BASH = shutil.which("bash") or "/bin/bash"

_CONV = "11111111-1111-1111-1111-111111111111"
_CONV_OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def env(tmp_path: Path, stub_mngr_log_sh: str) -> dict[str, Any]:
    """Stage the agent state dir (with mngr_log.sh stub + the real decoder) and app-data dir."""
    state_dir = tmp_path / "agent"
    commands = state_dir / "commands"
    commands.mkdir(parents=True)
    (commands / "mngr_log.sh").write_text(stub_mngr_log_sh)
    shutil.copy(_DECODER_PATH, commands / "decode_agy_transcript.py")
    app_data_dir = tmp_path / "app_data"
    (app_data_dir / "conversations").mkdir(parents=True)
    return {
        "state_dir": state_dir,
        "app_data_dir": app_data_dir,
        "conversation_ids_file": state_dir / "antigravity_conversation_ids",
        "raw_output_file": state_dir / "logs" / "antigravity_transcript" / "events.jsonl",
        "commands": commands,
    }


def _make_db(env: dict[str, Any], conv_id: str, rows: list[tuple[int, int, int, bytes]]) -> None:
    make_conversation_db(env["app_data_dir"] / "conversations" / f"{conv_id}.db", rows)


def _record_conversation(env: dict[str, Any], conv_id: str) -> None:
    with env["conversation_ids_file"].open("a") as handle:
        handle.write(conv_id + "\n")


def _run(env: dict[str, Any], path: str | None = None) -> subprocess.CompletedProcess[str]:
    proc_env = {
        **os.environ,
        "MNGR_AGENT_STATE_DIR": str(env["state_dir"]),
        "ANTIGRAVITY_APP_DATA_DIR": str(env["app_data_dir"]),
    }
    if path is not None:
        proc_env["PATH"] = path
    return subprocess.run([_BASH, str(_SCRIPT_PATH), "--single-pass"], env=proc_env, capture_output=True, text=True)


def _read(env: dict[str, Any]) -> list[dict[str, Any]]:
    output = env["raw_output_file"]
    if not output.exists():
        return []
    return [json.loads(line) for line in output.read_text().splitlines() if line.strip()]


def test_wrapper_decodes_db_into_records(env: dict[str, Any]) -> None:
    _make_db(
        env,
        _CONV,
        [(0, 14, 3, user_step("remember SECRET-9")), (2, 15, 3, assistant_step("noted SECRET-9"))],
    )
    _record_conversation(env, _CONV)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    events = _read(env)
    assert [event["type"] for event in events] == ["USER_INPUT", "PLANNER_RESPONSE"]
    assert all(event["_mngr_conv_id"] == _CONV for event in events)
    assert events[0]["content"] == "remember SECRET-9"
    assert events[1]["content"] == "noted SECRET-9"


def test_wrapper_only_reads_conversations_in_ids_file(env: dict[str, Any]) -> None:
    _make_db(env, _CONV, [(0, 14, 3, user_step("tracked"))])
    _make_db(env, _CONV_OTHER, [(0, 14, 3, user_step("foreign"))])
    _record_conversation(env, _CONV)
    assert _run(env).returncode == 0
    assert [event["content"] for event in _read(env)] == ["tracked"]


def test_wrapper_persists_offset_across_passes(env: dict[str, Any]) -> None:
    _make_db(env, _CONV, [(0, 14, 3, user_step("hi"))])
    _record_conversation(env, _CONV)
    assert _run(env).returncode == 0
    assert len(_read(env)) == 1
    # A second pass over the unchanged db emits nothing new (offset persisted).
    assert _run(env).returncode == 0
    assert len(_read(env)) == 1


def test_wrapper_fails_clearly_when_python3_is_missing(env: dict[str, Any], tmp_path: Path) -> None:
    # A real-ish mngr_log stub that surfaces log_error so we can assert the message.
    (env["commands"] / "mngr_log.sh").write_text(
        '#!/bin/bash\nlog_info() { :; }\nlog_debug() { :; }\nlog_warn() { :; }\nlog_error() { echo "$@" >&2; }\n'
    )
    _make_db(env, _CONV, [(0, 14, 3, user_step("hi"))])
    _record_conversation(env, _CONV)
    empty_path = tmp_path / "emptybin"
    empty_path.mkdir()
    # A PATH without python3; the guard runs entirely on bash builtins so it still fires.
    result = _run(env, path=str(empty_path))
    assert result.returncode == 1
    assert "python3" in result.stderr
    assert _read(env) == []
