"""The codex background-tasks supervisor, which every launch of a codex agent starts, clears the markers a previous process left."""

import importlib.resources
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME


@pytest.mark.tmux
def test_a_launch_clears_the_turn_and_dialog_markers_a_killed_codex_left(tmp_path: Path) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("01a1f3aa-2f4b-73f1-bd7d-8062a3beb1f2\n")
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()
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
