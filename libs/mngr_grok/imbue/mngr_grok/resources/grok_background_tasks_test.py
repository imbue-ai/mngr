import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.utils.parent_process import is_pid_alive
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import is_background_tasks_supervisor_polling
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_grok.resources.testing import SESSION_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import TRANSCRIPT_SCRIPT_NAMES
from imbue.mngr_grok.resources.testing import install_transcript_scripts
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.resources.testing import stage_fixture_sessions

_RESOURCES_DIR: Final[Path] = Path(__file__).parent
_SCRIPT_PATH: Final[Path] = _RESOURCES_DIR / "grok_background_tasks.sh"
_PLAIN_SESSION_DIR: Final[Path] = SESSION_FIXTURES_DIR / "plain" / "01a156d6-f927-7091-91a4-f6692a5800da"
_CHILD_SCRIPT_NAMES: Final[tuple[str, ...]] = ("stream_transcript.sh", "common_transcript.sh", "grok_usage.sh")
_FAST_POLL_SECONDS: Final[str] = "1"

# Records each start's pid, then stays alive like a long-lived child.
_STAND_IN_CHILD: Final[str] = """#!/usr/bin/env bash
set -euo pipefail
echo "$$" >> "$MNGR_AGENT_STATE_DIR/started_$(basename "$0")"
exec tail -f /dev/null
"""


def _install_stand_in_children(state_dir: Path, script_names: tuple[str, ...]) -> None:
    for script_name in script_names:
        (state_dir / "commands" / script_name).write_text(_STAND_IN_CHILD)


def _started_pids(state_dir: Path, script_name: str) -> list[int]:
    started_path = state_dir / "started_{}".format(script_name)
    if not started_path.exists():
        return []
    return [int(line) for line in started_path.read_text().split()]


def _supervisor_env(state_dir: Path, pane_id: str | None) -> dict[str, str]:
    return {
        **build_background_tasks_supervisor_env(state_dir, pane_id),
        "MNGR_GROK_SUPERVISOR_POLL_SECONDS": _FAST_POLL_SECONDS,
    }


def _pidfile(env: dict[str, str]) -> Path:
    return Path("/tmp/mngr_grok_{}.pid".format(env["MNGR_AGENT_ID"]))


def _read_supervisor_events_log(state_dir: Path) -> str:
    return (state_dir / "events" / "logs" / "grok_background_tasks" / "events.jsonl").read_text()


@pytest.mark.tmux
def test_supervisor_keeps_polling_when_launched_with_the_pre_rename_session_name(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    with renamed_tmux_session() as (old_name, _new_name, pane_id):
        with running_background_tasks_supervisor(
            _SCRIPT_PATH, [old_name], _supervisor_env(state_dir, pane_id)
        ) as process:
            assert is_background_tasks_supervisor_polling(process)

    assert "(session ended)" not in _read_supervisor_events_log(state_dir)


@pytest.mark.tmux
def test_supervisor_starts_every_present_child_and_restarts_one_that_died(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    _install_stand_in_children(state_dir, _CHILD_SCRIPT_NAMES)
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = _supervisor_env(state_dir, pane_id)
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], env):
            assert poll_until(
                lambda: all(len(_started_pids(state_dir, name)) == 1 for name in _CHILD_SCRIPT_NAMES), timeout=10.0
            )
            first_stream_pid = _started_pids(state_dir, "stream_transcript.sh")[0]
            os.kill(first_stream_pid, signal.SIGKILL)

            assert poll_until(lambda: len(_started_pids(state_dir, "stream_transcript.sh")) == 2, timeout=10.0)
            assert is_pid_alive(_started_pids(state_dir, "stream_transcript.sh")[1])
            assert len(_started_pids(state_dir, "common_transcript.sh")) == 1
            children = [_started_pids(state_dir, name)[-1] for name in _CHILD_SCRIPT_NAMES]
        assert poll_until(lambda: not any(is_pid_alive(pid) for pid in children), timeout=10.0)
        assert not _pidfile(env).exists()


@pytest.mark.tmux
def test_supervisor_starts_a_child_whose_script_appears_after_it_started(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        with running_background_tasks_supervisor(
            _SCRIPT_PATH, [new_name], _supervisor_env(state_dir, pane_id)
        ) as process:
            assert is_background_tasks_supervisor_polling(process)
            _install_stand_in_children(state_dir, ("common_transcript.sh",))

            assert poll_until(lambda: len(_started_pids(state_dir, "common_transcript.sh")) == 1, timeout=10.0)
            assert _started_pids(state_dir, "stream_transcript.sh") == []


@pytest.mark.tmux
def test_a_second_supervisor_for_the_same_agent_exits_at_once(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = _supervisor_env(state_dir, pane_id)
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], env) as first:
            assert is_background_tasks_supervisor_polling(first)

            second = subprocess.run(["bash", str(_SCRIPT_PATH), new_name], env=env, timeout=10, check=False)

            assert second.returncode == 0
            assert first.poll() is None
            assert _pidfile(env).read_text().strip() == str(first.pid)


@pytest.mark.tmux
def test_supervisors_of_different_agents_sharing_a_session_name_both_run(tmp_path: Path) -> None:
    with renamed_tmux_session() as (_old_name, new_name, _pane_id):
        first_env = _supervisor_env(create_background_tasks_state_dir(tmp_path / "a"), None)
        second_env = _supervisor_env(create_background_tasks_state_dir(tmp_path / "b"), None)
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], first_env) as first:
            assert is_background_tasks_supervisor_polling(first)
            with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], second_env) as second:
                assert is_background_tasks_supervisor_polling(second)


@pytest.mark.tmux
def test_a_pidfile_naming_an_unrelated_live_process_does_not_block_the_supervisor(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = _supervisor_env(state_dir, pane_id)
        _pidfile(env).write_text(str(os.getpid()))
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], env) as process:
            assert is_background_tasks_supervisor_polling(process)
            assert _pidfile(env).read_text().strip() == str(process.pid)


@pytest.mark.tmux
def test_supervisor_records_agent_activity_only_while_active(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    activity_path = state_dir / "activity" / "agent"
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        with running_background_tasks_supervisor(
            _SCRIPT_PATH, [new_name], _supervisor_env(state_dir, pane_id)
        ) as process:
            assert is_background_tasks_supervisor_polling(process)
            assert not activity_path.exists()

            (state_dir / "active").touch()
            assert poll_until(activity_path.exists, timeout=10.0)

    activity = json.loads(activity_path.read_text())
    assert activity["source"] == "activity_updater"
    assert isinstance(activity["time"], int) and activity["time"] > 1_700_000_000_000


@pytest.mark.tmux
def test_supervisor_exits_at_once_when_its_session_cannot_be_found(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    with renamed_tmux_session() as (old_name, _new_name, _pane_id):
        result = subprocess.run(
            ["bash", str(_SCRIPT_PATH), old_name],
            env=_supervisor_env(state_dir, None),
            timeout=10,
            check=False,
        )

    assert result.returncode == 0
    assert "(session ended)" in _read_supervisor_events_log(state_dir)


@pytest.mark.tmux
def test_supervisor_stops_its_children_once_its_session_ends(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    _install_stand_in_children(state_dir, _CHILD_SCRIPT_NAMES)
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = _supervisor_env(state_dir, pane_id)
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], env) as process:
            assert poll_until(
                lambda: all(len(_started_pids(state_dir, name)) == 1 for name in _CHILD_SCRIPT_NAMES), timeout=10.0
            )
            children = [_started_pids(state_dir, name)[0] for name in _CHILD_SCRIPT_NAMES]

            subprocess.run(["tmux", "kill-session", "-t", f"={new_name}"], check=True)

            assert process.wait(timeout=10) == 0
    assert poll_until(lambda: not any(is_pid_alive(pid) for pid in children), timeout=10.0)
    assert not _pidfile(env).exists()
    assert "(session ended)" in _read_supervisor_events_log(state_dir)


def test_supervisor_without_a_session_name_exits_with_usage(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        env={"PATH": os.environ["PATH"], "MNGR_AGENT_STATE_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "Usage: grok_background_tasks.sh <tmux_session_name>" in result.stderr


@pytest.mark.tmux
def test_supervised_streamer_and_converter_turn_a_session_into_both_transcripts(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    install_transcript_scripts(state_dir, TRANSCRIPT_SCRIPT_NAMES)
    stage_fixture_sessions(state_dir, "plain")
    raw_path = state_dir / "logs" / "grok_transcript" / "events.jsonl"
    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        with running_background_tasks_supervisor(_SCRIPT_PATH, [new_name], _supervisor_env(state_dir, pane_id)):
            assert poll_until(lambda: len(read_common_transcript_records(state_dir)) == 3, timeout=10.0)

    assert len(raw_path.read_text().splitlines()) == len(
        (_PLAIN_SESSION_DIR / "updates.jsonl").read_text().splitlines()
    )
    assert [record["type"] for record in read_common_transcript_records(state_dir)] == ["header", "step", "step"]
