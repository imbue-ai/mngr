import importlib.resources
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_grok.resources import grok_transcript_streamer as streamer
from imbue.mngr_grok.resources.testing import FIXTURE_SESSION_BUCKET
from imbue.mngr_grok.resources.testing import TRANSCRIPT_SCRIPT_NAMES
from imbue.mngr_grok.resources.testing import cut_before_first_turn_completed
from imbue.mngr_grok.resources.testing import install_transcript_scripts
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.resources.testing import read_complete_jsonl_records
from imbue.mngr_grok.resources.testing import run_agent_command_script
from imbue.mngr_grok.resources.testing import stage_fixture_sessions
from imbue.mngr_grok.resources.testing import tag_native_lines

_PLAIN_ROOT: Final[str] = "01a156d6-f927-7091-91a4-f6692a5800da"
_SEND_NOW_ROOT: Final[str] = "01a156d9-7190-72d3-acbb-7c88cc79e511"
_TOOL_ROOT: Final[str] = "01a156d9-1df2-7933-9165-d4ee061dc4d5"
_BACKGROUND_SUBAGENT_ROOT: Final[str] = "01a156db-07ea-7993-b2e6-dcaa7cb0a81c"
_BACKGROUND_SUBAGENT_CHILD: Final[str] = "01a156db-10ae-70c2-9a0f-a0dff6d5f376"
_FOREGROUND_ROOT: Final[str] = "6def8bc3-a90e-47b9-9511-9dd4240aafdc"
_FOREGROUND_CHILD: Final[str] = "01a156d9-9210-7181-8980-a9f04c0d6e80"
_FIRST_ROOT: Final[str] = "f603168f-7fec-4427-9b3f-f621a162e742"
_NEW_ROOT: Final[str] = "01a156dc-9ff8-7e32-a636-a71e85c2dfee"
_STOP_BLOCK_ROOT: Final[str] = "01a156df-6aaf-7473-adcc-b96e2650130d"

# Records argv and the prompt ids whose closing step is already in the common transcript.
_STAND_IN_HOOK: Final[str] = """#!/usr/bin/env python3
import json
import os
import sys

state_dir = os.environ["MNGR_AGENT_STATE_DIR"]
common_path = os.path.join(state_dir, "events", "grok", "common_transcript", "events.jsonl")
flushed_prompt_ids = []
if os.path.isfile(common_path):
    with open(common_path) as common_file:
        for line in common_file:
            prompt_id = (json.loads(line).get("extra") or {}).get("prompt_id")
            if prompt_id:
                flushed_prompt_ids.append(prompt_id)
with open(os.path.join(state_dir, "hook_calls.jsonl"), "a") as calls_file:
    calls_file.write(json.dumps({"argv": sys.argv[1:], "flushed_prompt_ids": flushed_prompt_ids}) + "\\n")
"""

# Stands in for `mngr destroy` removing the agent state directory once the first turn end is reported.
_STATE_DIR_REMOVING_HOOK: Final[str] = """#!/usr/bin/env python3
import os

state_dir = os.environ["MNGR_AGENT_STATE_DIR"]
os.rename(state_dir, state_dir + "-destroyed")
"""


def _stage_agent(tmp_path: Path, stub_mngr_log_sh: str, scenario: str) -> Path:
    """Lay out an agent state dir holding one fixture scenario's sessions, the scripts and a stand-in hook."""
    state_dir = tmp_path / "agent-state"
    install_transcript_scripts(state_dir, TRANSCRIPT_SCRIPT_NAMES, stub_mngr_log_sh)
    stage_fixture_sessions(state_dir, scenario)
    hook_path = state_dir / "plugin" / "grok" / "home" / "hooks" / "grok_hook.py"
    hook_path.parent.mkdir(parents=True)
    write_executable_script(hook_path, _STAND_IN_HOOK)
    return state_dir


def _session_file(state_dir: Path, session_id: str) -> Path:
    return streamer.sessions_dir_path(state_dir) / FIXTURE_SESSION_BUCKET / session_id / "updates.jsonl"


def _environ(state_dir: Path) -> dict[str, str]:
    return {**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)}


def _run_pass(state_dir: Path) -> None:
    streamer.run_stream_pass(state_dir, _environ(state_dir))


def _raw_lines(state_dir: Path) -> list[dict[str, Any]]:
    return read_complete_jsonl_records(state_dir / "logs" / "grok_transcript" / "events.jsonl")


def _hook_calls(state_dir: Path) -> list[dict[str, Any]]:
    return read_complete_jsonl_records(state_dir / "hook_calls.jsonl")


def _hook_argvs(state_dir: Path) -> list[list[str]]:
    return [call["argv"] for call in _hook_calls(state_dir)]


def _hook_failure_events(state_dir: Path) -> list[dict[str, Any]]:
    return read_complete_jsonl_records(state_dir / "events" / "grok" / "hook_failures" / "events.jsonl")


def _expected_raw_lines(path: Path, session_id: str, parent_session_id: str | None) -> list[dict[str, Any]]:
    return tag_native_lines(session_id, parent_session_id, path.read_bytes().splitlines(keepends=True))


def _prompt_ids_of_turn_completed(path: Path) -> list[str]:
    prompt_ids: list[str] = []
    for line in path.read_bytes().splitlines():
        update = json.loads(line)["params"]["update"]
        if update["sessionUpdate"] == "turn_completed":
            prompt_ids.append(update["prompt_id"])
    return prompt_ids


def test_single_pass_appends_every_root_line_tagged_with_its_session_and_byte_offset(
    tmp_path: Path, stub_mngr_log_sh: str
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    native_path = _session_file(state_dir, _PLAIN_ROOT)

    _run_pass(state_dir)

    assert _raw_lines(state_dir) == _expected_raw_lines(native_path, _PLAIN_ROOT, None)
    raw_text = (state_dir / "logs" / "grok_transcript" / "events.jsonl").read_bytes()
    for native_line in native_path.read_bytes().splitlines():
        assert b'"record":' + native_line + b"}\n" in raw_text


def test_turn_completed_is_reported_after_the_common_transcript_holds_the_closing_step(
    tmp_path: Path, stub_mngr_log_sh: str
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "send_now")
    cut_prompt, follow_up_prompt = _prompt_ids_of_turn_completed(_session_file(state_dir, _SEND_NOW_ROOT))

    _run_pass(state_dir)

    calls = _hook_calls(state_dir)
    assert [call["argv"] for call in calls] == [
        ["--turn-completed", _SEND_NOW_ROOT, cut_prompt],
        ["--turn-completed", _SEND_NOW_ROOT, follow_up_prompt],
    ]
    for call in calls:
        assert call["argv"][2] in call["flushed_prompt_ids"]


def test_child_sessions_are_streamed_with_their_parent_and_never_report_turn_ends(
    tmp_path: Path, stub_mngr_log_sh: str
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "background_subagent")

    _run_pass(state_dir)

    raw_lines = _raw_lines(state_dir)
    child_lines = [line for line in raw_lines if line["session_id"] == _BACKGROUND_SUBAGENT_CHILD]
    assert child_lines == _expected_raw_lines(
        _session_file(state_dir, _BACKGROUND_SUBAGENT_CHILD), _BACKGROUND_SUBAGENT_CHILD, _BACKGROUND_SUBAGENT_ROOT
    )
    assert {argv[1] for argv in _hook_argvs(state_dir)} == {_BACKGROUND_SUBAGENT_ROOT}
    assert [argv[2] for argv in _hook_argvs(state_dir)] == _prompt_ids_of_turn_completed(
        _session_file(state_dir, _BACKGROUND_SUBAGENT_ROOT)
    )


def test_a_child_session_written_after_its_spawn_record_is_picked_up_by_a_later_pass(
    tmp_path: Path, stub_mngr_log_sh: str
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "background_subagent")
    child_path = _session_file(state_dir, _BACKGROUND_SUBAGENT_CHILD)
    child_bytes = child_path.read_bytes()
    child_path.unlink()

    _run_pass(state_dir)
    assert _raw_lines(state_dir) == _expected_raw_lines(
        _session_file(state_dir, _BACKGROUND_SUBAGENT_ROOT), _BACKGROUND_SUBAGENT_ROOT, None
    )

    child_path.write_bytes(child_bytes)
    _run_pass(state_dir)

    child_lines = [line for line in _raw_lines(state_dir) if line["session_id"] == _BACKGROUND_SUBAGENT_CHILD]
    assert child_lines == _expected_raw_lines(child_path, _BACKGROUND_SUBAGENT_CHILD, _BACKGROUND_SUBAGENT_ROOT)


def test_a_root_turn_end_flush_holds_a_running_child_step_open(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "background_subagent")
    child_path = _session_file(state_dir, _BACKGROUND_SUBAGENT_CHILD)
    child_bytes = child_path.read_bytes()
    child_path.write_bytes(cut_before_first_turn_completed(child_bytes))

    _run_pass(state_dir)
    child_sources = [
        record["source"]
        for record in read_common_transcript_records(state_dir)
        if record.get("extra", {}).get("is_sidechain")
    ]
    assert child_sources == ["user"]

    child_path.write_bytes(child_bytes)
    _run_pass(state_dir)
    run_agent_command_script(state_dir, "common_transcript.sh", "--single-pass", "--hold-open-steps")

    child_steps = [
        record for record in read_common_transcript_records(state_dir) if record.get("extra", {}).get("is_sidechain")
    ]
    assert [step["source"] for step in child_steps] == ["user", "agent"]
    assert child_steps[1]["extra"]["finish_reason"] == "end_turn"
    assert child_steps[1]["metrics"]["prompt_tokens"] == 32


def test_failed_mngr_hook_runs_in_root_and_child_sessions_are_recorded_as_events(
    tmp_path: Path, stub_mngr_log_sh: str
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "foreground_subagent_failing_hooks")

    streamer.run_stream_pass(
        state_dir, {**_environ(state_dir), "MNGR_AGENT_ID": "agent-0123", "MNGR_AGENT_NAME": "grok-reviewer"}
    )

    events = _hook_failure_events(state_dir)
    assert [(event["session_id"], event["hook_event"]) for event in events] == [
        (_FOREGROUND_ROOT, "session_start"),
        (_FOREGROUND_ROOT, "user_prompt_submit"),
        (_FOREGROUND_ROOT, "stop"),
        (_FOREGROUND_ROOT, "user_prompt_submit"),
        (_FOREGROUND_ROOT, "pre_tool_use"),
        (_FOREGROUND_ROOT, "post_tool_use"),
        (_FOREGROUND_ROOT, "stop"),
        (_FOREGROUND_ROOT, "user_prompt_submit"),
        (_FOREGROUND_ROOT, "pre_tool_use"),
        (_FOREGROUND_ROOT, "post_tool_use"),
        (_FOREGROUND_ROOT, "stop"),
        (_FOREGROUND_ROOT, "session_end"),
        (_FOREGROUND_ROOT, "stop"),
        (_FOREGROUND_CHILD, "user_prompt_submit"),
        (_FOREGROUND_CHILD, "subagent_stop"),
        (_FOREGROUND_CHILD, "session_end"),
    ]
    first_event = events[0]
    assert first_event["type"] == "hook_failed"
    assert first_event["source"] == "grok/hook_failures"
    assert first_event["hook_name"] == "global/mngr:session_start[0].hooks[0]"
    assert first_event["error"] == "exit code 1: NOISY_STDERR event=session_start"
    assert events[13]["parent_session_id"] == _FOREGROUND_ROOT
    assert len({event["event_id"] for event in events}) == len(events)
    assert {(event["agent_id"], event["agent_name"]) for event in events} == {("agent-0123", "grok-reviewer")}


@pytest.mark.parametrize(
    ("hook_file_stem", "is_blocked"),
    [pytest.param("stopblock", False, id="foreign_unblocked"), pytest.param("mngr", True, id="mngr_blocked")],
)
def test_blocked_or_foreign_hook_runs_are_not_recorded_as_failures(
    tmp_path: Path, stub_mngr_log_sh: str, hook_file_stem: str, is_blocked: bool
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "stop_block")
    session_path = _session_file(state_dir, _STOP_BLOCK_ROOT)
    native_bytes = session_path.read_bytes().replace(
        b'"global/stopblock:', '"global/{}:'.format(hook_file_stem).encode()
    )
    if not is_blocked:
        native_bytes = native_bytes.replace(b'"blocked":true', b'"blocked":false')
    session_path.write_bytes(native_bytes)

    _run_pass(state_dir)

    assert '"global/{}:'.format(hook_file_stem).encode() in session_path.read_bytes()
    assert b'"status":"failed"' in session_path.read_bytes()
    assert (b'"blocked":true' in session_path.read_bytes()) is is_blocked
    assert _hook_failure_events(state_dir) == []


def test_a_second_pass_appends_only_what_was_written_after_the_first(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "tool")
    session_path = _session_file(state_dir, _TOOL_ROOT)
    full_bytes = session_path.read_bytes()
    session_path.write_bytes(cut_before_first_turn_completed(full_bytes))

    _run_pass(state_dir)
    assert _hook_argvs(state_dir) == []

    session_path.write_bytes(full_bytes)
    _run_pass(state_dir)
    _run_pass(state_dir)

    assert _raw_lines(state_dir) == _expected_raw_lines(session_path, _TOOL_ROOT, None)
    assert _hook_argvs(state_dir) == [["--turn-completed", _TOOL_ROOT, _prompt_ids_of_turn_completed(session_path)[0]]]


def test_a_torn_last_line_waits_for_its_newline(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    session_path = _session_file(state_dir, _PLAIN_ROOT)
    full_bytes = session_path.read_bytes()
    last_line_start = full_bytes.rindex(b"\n", 0, len(full_bytes) - 1) + 1
    session_path.write_bytes(full_bytes[: last_line_start + 20])

    _run_pass(state_dir)
    assert [line["offset"] for line in _raw_lines(state_dir)][-1] < last_line_start
    state = json.loads(streamer.streamer_state_path(state_dir).read_text())
    assert (
        state["files"]["{}/{}/updates.jsonl".format(FIXTURE_SESSION_BUCKET, _PLAIN_ROOT)]["offset"] == last_line_start
    )
    assert not streamer.has_unread_bytes(streamer.sessions_dir_path(state_dir), [_PLAIN_ROOT], state)

    session_path.write_bytes(full_bytes)
    _run_pass(state_dir)

    assert _raw_lines(state_dir) == _expected_raw_lines(session_path, _PLAIN_ROOT, None)


def test_a_line_that_is_not_json_is_carried_unparsed(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    session_path = _session_file(state_dir, _PLAIN_ROOT)
    garbage_offset = session_path.stat().st_size
    with session_path.open("ab") as session_file:
        session_file.write(b'{"timestamp": 1792458400, "method": tor\n')

    _run_pass(state_dir)

    assert _raw_lines(state_dir)[-1] == {
        "session_id": _PLAIN_ROOT,
        "parent_session_id": None,
        "offset": garbage_offset,
        "unparsed": '{"timestamp": 1792458400, "method": tor',
    }


def test_raw_output_torn_by_a_crash_gets_its_own_line_before_new_lines(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    raw_path = state_dir / "logs" / "grok_transcript" / "events.jsonl"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_bytes(b'{"session_id":"torn-by-a-kil')

    _run_pass(state_dir)

    raw_text_lines = raw_path.read_bytes().splitlines()
    assert raw_text_lines[0] == b'{"session_id":"torn-by-a-kil'
    assert [json.loads(line) for line in raw_text_lines[1:]] == _expected_raw_lines(
        _session_file(state_dir, _PLAIN_ROOT), _PLAIN_ROOT, None
    )


def test_a_session_file_that_shrank_is_streamed_again_from_its_start(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    session_path = _session_file(state_dir, _PLAIN_ROOT)
    _run_pass(state_dir)
    first_line = session_path.read_bytes().splitlines(keepends=True)[0]

    session_path.write_bytes(first_line)
    _run_pass(state_dir)

    assert _raw_lines(state_dir)[-1]["offset"] == 0
    assert _raw_lines(state_dir)[-1]["record"] == json.loads(first_line)


def test_new_session_and_resumed_root_are_both_streamed(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "new_session_and_resume")

    _run_pass(state_dir)

    first_path = _session_file(state_dir, _FIRST_ROOT)
    new_path = _session_file(state_dir, _NEW_ROOT)
    assert _raw_lines(state_dir) == _expected_raw_lines(first_path, _FIRST_ROOT, None) + _expected_raw_lines(
        new_path, _NEW_ROOT, None
    )
    assert _hook_argvs(state_dir) == [
        ["--turn-completed", _FIRST_ROOT, prompt_id] for prompt_id in _prompt_ids_of_turn_completed(first_path)
    ] + [["--turn-completed", _NEW_ROOT, prompt_id] for prompt_id in _prompt_ids_of_turn_completed(new_path)]


def test_records_a_fork_copied_from_its_source_trigger_no_hook_calls(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "foreground_subagent_failing_hooks")
    session_path = _session_file(state_dir, _FOREGROUND_ROOT)
    session_path.write_bytes(
        session_path.read_bytes().replace(
            '"eventId":"{}-'.format(_FOREGROUND_ROOT).encode(), b'"eventId":"0a0a0a0a-source-session-'
        )
    )

    _run_pass(state_dir)

    assert len(_raw_lines(state_dir)) == len(session_path.read_bytes().splitlines())
    assert _hook_argvs(state_dir) == []
    assert _hook_failure_events(state_dir) == []


def test_single_pass_script_defers_while_the_convert_lock_is_held(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    lock_dir = streamer.convert_lock_path(state_dir)
    lock_dir.mkdir()

    run_agent_command_script(
        state_dir, "stream_transcript.sh", "--single-pass", extra_env={"MNGR_CONVERT_LOCK_TIMEOUT": "0"}
    )
    assert _raw_lines(state_dir) == []
    assert lock_dir.is_dir()

    lock_dir.rmdir()
    run_agent_command_script(state_dir, "stream_transcript.sh", "--single-pass")

    assert _raw_lines(state_dir) == _expected_raw_lines(_session_file(state_dir, _PLAIN_ROOT), _PLAIN_ROOT, None)
    assert not lock_dir.exists()
    assert len(_hook_argvs(state_dir)) == 1


def test_a_stale_convert_lock_is_broken(tmp_path: Path) -> None:
    lock_dir = streamer.convert_lock_path(tmp_path)
    lock_dir.mkdir()
    stale_time = lock_dir.stat().st_mtime - 120
    os.utime(lock_dir, (stale_time, stale_time))

    assert streamer.acquire_convert_lock(lock_dir, timeout_seconds=0)
    assert lock_dir.stat().st_mtime > stale_time + 60


def test_a_held_lock_blocks_the_shell_library_until_released(tmp_path: Path) -> None:
    state_dir = tmp_path / "agent-state"
    state_dir.mkdir()
    lock_path = streamer.convert_lock_path(state_dir)
    library = importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh")
    acquire_once = 'source "$1" && MNGR_CONVERT_LOCK_TIMEOUT=0 mngr_common_transcript_acquire_lock'
    with importlib.resources.as_file(library) as library_path:
        assert streamer.acquire_convert_lock(lock_path, timeout_seconds=0)
        held = subprocess.run(
            ["bash", "-c", acquire_once, "_", str(library_path)], env=_environ(state_dir), check=False, timeout=10
        )
        streamer.release_convert_lock(lock_path)
        released = subprocess.run(
            ["bash", "-c", acquire_once, "_", str(library_path)], env=_environ(state_dir), check=False, timeout=10
        )
    assert held.returncode == 1
    assert released.returncode == 0
    assert not streamer.acquire_convert_lock(lock_path, timeout_seconds=0)


def test_daemon_and_single_passes_stream_each_line_exactly_once(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "stop_block")
    session_path = _session_file(state_dir, _STOP_BLOCK_ROOT)
    lines = session_path.read_bytes().splitlines(keepends=True)
    session_path.write_bytes(b"".join(lines[:5]))
    daemon = subprocess.Popen(
        ["bash", str(state_dir / "commands" / "stream_transcript.sh")],
        env=_environ(state_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert poll_until(lambda: len(_raw_lines(state_dir)) == 5, timeout=4.0)
        with session_path.open("ab") as session_file:
            session_file.write(b"".join(lines[5:12]))
        assert poll_until(lambda: len(_raw_lines(state_dir)) == 12, timeout=4.0)
        for chunk_end in (20, len(lines)):
            with session_path.open("ab") as session_file:
                session_file.write(b"".join(lines[len(session_path.read_bytes().splitlines()) : chunk_end]))
            run_agent_command_script(state_dir, "stream_transcript.sh", "--single-pass")
        assert poll_until(lambda: len(_raw_lines(state_dir)) >= len(lines), timeout=4.0)
        assert poll_until(lambda: len(_hook_argvs(state_dir)) == 3, timeout=4.0)
        assert daemon.poll() is None
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=10)

    assert _raw_lines(state_dir) == _expected_raw_lines(session_path, _STOP_BLOCK_ROOT, None)
    assert len(_hook_argvs(state_dir)) == 3
    assert not streamer.convert_lock_path(state_dir).exists()


def test_daemon_exits_once_the_agent_state_dir_is_removed(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    turn_count = len(_prompt_ids_of_turn_completed(_session_file(state_dir, _PLAIN_ROOT)))
    daemon = subprocess.Popen(
        ["bash", str(state_dir / "commands" / "stream_transcript.sh")],
        env=_environ(state_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert poll_until(lambda: len(_hook_argvs(state_dir)) == turn_count, timeout=5.0)
        # The daemon may still be writing into the state dir, so it leaves its path in one step.
        state_dir.rename(tmp_path / "destroyed-agent-state")
        assert daemon.wait(timeout=3) == 0
    finally:
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait(timeout=10)


def test_single_pass_streamer_runs_one_pass(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")

    streamer.run_streamer(state_dir, True, _environ(state_dir))

    assert _raw_lines(state_dir) == _expected_raw_lines(_session_file(state_dir, _PLAIN_ROOT), _PLAIN_ROOT, None)
    assert len(_hook_argvs(state_dir)) == 1


def test_daemon_streamer_streams_until_the_agent_state_dir_is_removed(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    native_path = _session_file(state_dir, _PLAIN_ROOT)
    expected_lines = _expected_raw_lines(native_path, _PLAIN_ROOT, None)
    (state_dir / "plugin" / "grok" / "home" / "hooks" / "grok_hook.py").write_text(_STATE_DIR_REMOVING_HOOK)

    streamer.run_streamer(state_dir, False, _environ(state_dir))

    destroyed_state_dir = tmp_path / "agent-state-destroyed"
    assert not state_dir.exists()
    assert _raw_lines(destroyed_state_dir) == expected_lines


def test_roots_without_session_files_stream_nothing(tmp_path: Path) -> None:
    state_dir = tmp_path / "agent-state"
    (state_dir / "plugin" / "grok").mkdir(parents=True)
    (state_dir / "plugin" / "grok" / "roots").write_text(_PLAIN_ROOT + "\n")

    _run_pass(state_dir)

    assert sorted(path.name for path in (state_dir / "plugin" / "grok").iterdir()) == ["roots"]
    assert _raw_lines(state_dir) == []


def test_a_session_file_removed_after_streaming_leaves_nothing_unread(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    _run_pass(state_dir)
    raw_lines_before = _raw_lines(state_dir)

    _session_file(state_dir, _PLAIN_ROOT).unlink()
    _run_pass(state_dir)

    state = streamer.load_streamer_state(streamer.streamer_state_path(state_dir))
    assert not streamer.has_unread_bytes(streamer.sessions_dir_path(state_dir), [_PLAIN_ROOT], state)
    assert _raw_lines(state_dir) == raw_lines_before


def test_a_root_listed_twice_is_streamed_once(tmp_path: Path, stub_mngr_log_sh: str) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")

    follow_ups = streamer.stream_new_lines_locked(state_dir, [_PLAIN_ROOT, _PLAIN_ROOT], _environ(state_dir))

    assert _raw_lines(state_dir) == _expected_raw_lines(_session_file(state_dir, _PLAIN_ROOT), _PLAIN_ROOT, None)
    assert len(follow_ups["turn_completions"]) == 1


def test_a_pass_that_cannot_take_the_convert_lock_is_deferred_and_logged(
    tmp_path: Path, stub_mngr_log_sh: str, caplog: pytest.LogCaptureFixture
) -> None:
    state_dir = _stage_agent(tmp_path, stub_mngr_log_sh, "plain")
    lock_dir = streamer.convert_lock_path(state_dir)
    lock_dir.mkdir()

    with caplog.at_level(logging.WARNING, logger="grok_transcript_streamer"):
        streamer.run_stream_pass(state_dir, {**_environ(state_dir), "MNGR_CONVERT_LOCK_TIMEOUT": "0"})

    assert [record.getMessage() for record in caplog.records] == [
        "Deferred a pass: the convert lock at {} stayed held".format(lock_dir)
    ]
    assert _raw_lines(state_dir) == []
    assert _hook_argvs(state_dir) == []
    assert lock_dir.is_dir()


def test_a_held_convert_lock_is_waited_on_until_the_timeout(tmp_path: Path) -> None:
    lock_dir = streamer.convert_lock_path(tmp_path)
    lock_dir.mkdir()

    started_at = time.monotonic()
    is_acquired = streamer.acquire_convert_lock(lock_dir, timeout_seconds=0.25)
    waited_seconds = time.monotonic() - started_at

    assert is_acquired is False
    assert 0.25 <= waited_seconds < 5
    assert lock_dir.is_dir()


def test_releasing_a_lock_that_was_already_broken_leaves_it_free(tmp_path: Path) -> None:
    lock_dir = streamer.convert_lock_path(tmp_path)
    assert streamer.acquire_convert_lock(lock_dir, timeout_seconds=0)
    lock_dir.rmdir()

    streamer.release_convert_lock(lock_dir)

    assert not lock_dir.exists()
    assert streamer.acquire_convert_lock(lock_dir, timeout_seconds=0)


def _ignore_signal(signal_number: int, frame: object) -> None:
    return None


def test_a_pending_stop_signal_ends_the_wait_for_a_held_lock_at_once(tmp_path: Path) -> None:
    lock_dir = streamer.convert_lock_path(tmp_path)
    lock_dir.mkdir()
    previous_handler = signal.signal(signal.SIGTERM, _ignore_signal)
    try:
        with streamer.termination_signals_deferred():
            is_pending_before = streamer.is_termination_pending()
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            is_pending_after = streamer.is_termination_pending()
            started_at = time.monotonic()
            is_acquired = streamer.acquire_convert_lock(lock_dir, timeout_seconds=30.0)
            waited_seconds = time.monotonic() - started_at
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    assert (is_pending_before, is_pending_after, is_acquired) == (False, True, False)
    assert waited_seconds < 5


_STATE_FILE_ENTRY: Final[dict[str, Any]] = {"offset": 7, "session_id": "s", "parent_session_id": None}


@pytest.mark.parametrize(
    ("state_bytes", "expected_state"),
    [
        pytest.param(b"{not json", streamer.new_streamer_state(), id="not_json"),
        pytest.param(b'{"files": {"\xff": 1}}', streamer.new_streamer_state(), id="not_utf8"),
        pytest.param(b"[]", streamer.new_streamer_state(), id="not_an_object"),
        pytest.param(b'{"files": []}', streamer.new_streamer_state(), id="files_not_an_object"),
        pytest.param(
            json.dumps(
                {
                    "files": {
                        "a/s/updates.jsonl": {"offset": 7},
                        "b/s/updates.jsonl": {**_STATE_FILE_ENTRY, "offset": -1},
                        "c/s/updates.jsonl": {**_STATE_FILE_ENTRY, "parent_session_id": "../p"},
                        "d/s/updates.jsonl": _STATE_FILE_ENTRY,
                    },
                }
            ).encode(),
            {"files": {"d/s/updates.jsonl": _STATE_FILE_ENTRY}},
            id="malformed_entries",
        ),
    ],
)
def test_streamer_state_of_an_unexpected_shape_is_reset_or_repaired(
    tmp_path: Path, state_bytes: bytes, expected_state: dict[str, Any]
) -> None:
    state_path = tmp_path / "streamer_state.json"
    state_path.write_bytes(state_bytes)

    assert streamer.load_streamer_state(state_path) == expected_state


def _native_update_record(update: dict[str, Any]) -> dict[str, Any]:
    return {"params": {"update": update}}


@pytest.mark.parametrize(
    "runs",
    [
        pytest.param(None, id="no_runs"),
        pytest.param("global/mngr:stop[0].hooks[0]", id="runs_not_a_list"),
        pytest.param([{"name": "global/mngr:stop[0].hooks[0]", "status": "failed"}], id="status_not_an_object"),
        pytest.param([{"name": 7, "status": {"status": "failed"}}], id="name_not_a_string"),
    ],
)
def test_malformed_hook_execution_runs_record_no_failures(runs: object) -> None:
    follow_ups = streamer.new_follow_ups()
    record = _native_update_record({"sessionUpdate": "hook_execution", "event_name": "stop", "runs": runs})

    streamer.scan_native_record(record, "b/s/updates.jsonl", "s1", None, 0, streamer.new_streamer_state(), follow_ups)

    assert follow_ups == streamer.new_follow_ups()


@pytest.mark.parametrize(
    ("record", "expected_seconds"),
    [
        pytest.param({"params": {"_meta": {"agentTimestampMs": 1792458294289}}}, 1792458294.289, id="agent_ms"),
        pytest.param(
            {"params": {"_meta": {"agentTimestampMs": True}}, "timestamp": 1792458300}, 1792458300.0, id="seconds"
        ),
    ],
)
def test_record_time_prefers_the_agent_milliseconds_then_the_record_seconds(
    record: dict[str, Any], expected_seconds: float
) -> None:
    assert streamer.record_epoch_seconds(record) == pytest.approx(expected_seconds)


def test_record_time_without_a_timestamp_is_now() -> None:
    before = time.time()

    record_seconds = streamer.record_epoch_seconds({"params": {"_meta": {}}, "timestamp": "yesterday"})

    assert before <= record_seconds <= time.time()


def test_configured_logging_appends_mngr_log_lines_under_the_agent_state(tmp_path: Path) -> None:
    logger = logging.getLogger("grok_transcript_streamer")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        streamer.configure_logging(tmp_path)
        streamer.read_lock_timeout_seconds({"MNGR_CONVERT_LOCK_TIMEOUT": "soon"})
    finally:
        for handler in [handler for handler in logger.handlers if handler not in saved_handlers]:
            handler.close()
            logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate

    log_path = tmp_path / "events" / "logs" / "stream_transcript" / "events.jsonl"
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [(line["source"], line["level"], line["message"]) for line in lines] == [
        ("logs/stream_transcript", "WARNING", "Ignored non-numeric MNGR_CONVERT_LOCK_TIMEOUT='soon'")
    ]


@pytest.mark.parametrize(
    ("roots_text", "expected_ids"),
    [
        ("", []),
        ("a1\n\n a1 \nb2\n", ["a1", "b2"]),
        ("../escape\n.hidden\nok-1.2_x\n", ["ok-1.2_x"]),
    ],
)
def test_root_session_ids_are_deduplicated_and_validated(
    tmp_path: Path, roots_text: str, expected_ids: list[str]
) -> None:
    roots_path = tmp_path / "roots"
    roots_path.write_text(roots_text)

    assert streamer.read_root_session_ids(roots_path) == expected_ids


def test_missing_roots_file_means_no_sessions(tmp_path: Path) -> None:
    assert streamer.read_root_session_ids(tmp_path / "roots") == []


@pytest.mark.parametrize(
    ("event_id", "expected"),
    [("s1-4", False), ("other-4", True), (None, False)],
)
def test_copied_records_are_recognised_by_their_event_id_prefix(event_id: str | None, expected: bool) -> None:
    meta = {} if event_id is None else {"eventId": event_id}
    record = {"params": {"sessionId": "s1", "_meta": meta}}

    assert streamer.is_copied_record(record, "s1") is expected


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, streamer.DEFAULT_LOCK_TIMEOUT_SECONDS),
        ({"MNGR_CONVERT_LOCK_TIMEOUT": "2"}, 2.0),
        ({"MNGR_CONVERT_LOCK_TIMEOUT": "x"}, streamer.DEFAULT_LOCK_TIMEOUT_SECONDS),
    ],
)
def test_lock_timeout_comes_from_the_environment(environ: dict[str, str], expected: float) -> None:
    assert streamer.read_lock_timeout_seconds(environ) == expected


def test_event_timestamps_have_nanosecond_width() -> None:
    assert streamer.format_event_timestamp(1792458294.289) == "2026-10-20T01:04:54.289000000Z"


def test_a_failing_follow_up_command_is_logged_and_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="grok_transcript_streamer"):
        streamer.run_follow_up_command(["bash", "-c", "echo broken >&2; exit 3"], dict(os.environ))
        streamer.run_follow_up_command([str(tmp_path / "missing_hook.py"), "--turn-completed", "s", "p"], {})

    messages = [record.getMessage() for record in caplog.records]
    assert any("exited 3: broken" in message for message in messages)
    assert any(message.startswith("Failed to run") and "missing_hook.py" in message for message in messages)


def test_log_lines_use_the_mngr_log_shape() -> None:
    record = logging.LogRecord(
        "grok_transcript_streamer", logging.WARNING, __file__, 1, "Deferred %s", ("a pass",), None
    )

    line = json.loads(streamer._JsonlLogFormatter().format(record))

    assert {key: line[key] for key in ("type", "source", "level", "message")} == {
        "type": "stream_transcript",
        "source": "logs/stream_transcript",
        "level": "WARNING",
        "message": "Deferred a pass",
    }
    assert line["event_id"].startswith("evt-") and line["timestamp"].endswith("Z")
