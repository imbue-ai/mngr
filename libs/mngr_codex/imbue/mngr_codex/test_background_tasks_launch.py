"""The codex background-tasks supervisor, which every launch of a codex agent starts: what it clears, and the records it keeps."""

import importlib.resources
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from imbue.mngr import resources as _mngr_resources
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_codex.codex_config import RECORD_BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.resources.record_background_tasks import RECORD_DIR_NAME
from imbue.mngr_codex.resources.testing import FakeCodexAppServer
from imbue.mngr_codex.resources.testing import codex_background_terminal
from imbue.mngr_codex.resources.testing import codex_thread
from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import serving_fake_codex_app_server_for_agent


def _provision_record_writer(state_dir: Path) -> None:
    """Install the record writer and the shared record library into the agent's commands/."""
    provision_commands_dir(state_dir, [RECORD_BACKGROUND_TASKS_SCRIPT_NAME])
    library = importlib.resources.files(_mngr_resources) / "mngr_background_task_lib.sh"
    (state_dir / "commands" / "mngr_background_task_lib.sh").write_text(library.read_text())


@pytest.mark.tmux
def test_a_launch_clears_the_markers_and_records_a_killed_codex_left_and_no_one_elses(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    _provision_record_writer(state_dir)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("01a1f3aa-2f4b-73f1-bd7d-8062a3beb1f2\n")
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()
    record_dir = state_dir / RECORD_DIR_NAME
    record_dir.mkdir()
    (record_dir / "task-exec-0b6c2d1e").write_text("seen:1795099000 kind:SHELL native_type:backgroundTerminals\n")
    (record_dir / "lease-other-writer").write_text("seen:1795099000\n")
    script = importlib.resources.files(_codex_resources) / "codex_background_tasks.sh"

    # The resource guard sees the supervisor's tmux calls through its own variables.
    guard_env = {name: value for name, value in os.environ.items() if name.startswith("_PYTEST_GUARD_")}
    with importlib.resources.as_file(script) as script_path:
        subprocess.run(
            ["bash", str(script_path), "mngr-no-such-session"],
            env={**build_background_tasks_supervisor_env(state_dir, None), **guard_env},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

    assert not (state_dir / ACTIVE_MARKER_FILENAME).exists()
    assert not (state_dir / PERMISSIONS_WAITING_FILENAME).exists()
    assert sorted(path.name for path in record_dir.iterdir()) == ["lease-other-writer"]


@pytest.mark.tmux
def test_a_launch_hands_the_transcript_streamer_its_launch_time(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    seen_launch = tmp_path / "seen_launch"
    (state_dir / "commands" / "stream_transcript.sh").write_text(
        f'printf "%s" "${{MNGR_CODEX_LAUNCHED_AT:-unset}}" > {shlex.quote(str(seen_launch))}\nexec sleep 58317\n'
    )
    script = importlib.resources.files(_codex_resources) / "codex_background_tasks.sh"
    before = int(time.time())

    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = build_background_tasks_supervisor_env(state_dir, pane_id)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [new_name], env):
                assert poll_until(lambda: seen_launch.exists() and seen_launch.read_text() != "", timeout=10), (
                    "the supervisor never started the transcript streamer"
                )

    assert before <= int(seen_launch.read_text()) <= int(time.time())


_ROOT_THREAD = "01a1f2b2-6d9c-7b93-87cc-4beb530e40ef"
_TERMINAL = "exec-5d2a9c4e-0b7f-4e1a-9c3d-6f8e2b1a7c90"


@pytest.mark.tmux
def test_a_running_supervisor_records_what_its_app_server_lists_and_drops_it_once_gone(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    _provision_record_writer(state_dir)
    record_path = state_dir / RECORD_DIR_NAME / f"task-{_TERMINAL}"
    script = importlib.resources.files(_codex_resources) / "codex_background_tasks.sh"
    server = FakeCodexAppServer(
        threads=(codex_thread(_ROOT_THREAD),),
        background_terminals={_ROOT_THREAD: (codex_background_terminal(_TERMINAL, command="sleep 52816"),)},
    )

    with renamed_tmux_session() as (_old_name, new_name, pane_id):
        env = build_background_tasks_supervisor_env(state_dir, pane_id)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [new_name], env):
                with serving_fake_codex_app_server_for_agent(state_dir, server):
                    assert poll_until(record_path.exists, timeout=10), "the supervisor never wrote the record"
                    recorded_line = record_path.read_text()
                is_dropped = poll_until(lambda: not record_path.exists(), timeout=10)

    assert recorded_line.split(" ", 1)[1] == "kind:SHELL native_type:backgroundTerminals\n"
    assert is_dropped
