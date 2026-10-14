"""A renamed codex agent's background tasks and app-server window keep finding its session.

Renaming an agent renames its tmux session, so the session name a supervisor was
launched with goes stale. The supervisor therefore identifies its session through the
pane it runs in (``$TMUX_PANE``) and keys its pidfile on the agent id. A launch
command an older mngr stored spawns the app-server window into the session's name,
so start upgrades that spawn to read its session when it runs.
"""

import importlib.resources
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import is_background_tasks_supervisor_polling
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.plugin import upgrade_app_server_window_spawn


@pytest.mark.tmux
def test_codex_background_tasks_keeps_polling_when_launched_with_the_pre_rename_session_name(
    tmp_path: Path,
) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    script = importlib.resources.files(_codex_resources) / "codex_background_tasks.sh"
    with renamed_tmux_session() as (old_name, _new_name, pane_id):
        env = build_background_tasks_supervisor_env(state_dir, pane_id)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [old_name], env) as process:
                assert is_background_tasks_supervisor_polling(process)

    events_log = state_dir / "events" / "logs" / "codex_background_tasks" / "events.jsonl"
    assert "(session ended)" not in events_log.read_text()


@pytest.mark.tmux
def test_codex_background_tasks_for_different_agents_sharing_a_session_name_both_run(
    tmp_path: Path,
) -> None:
    script = importlib.resources.files(_codex_resources) / "codex_background_tasks.sh"
    with renamed_tmux_session() as (_old_name, new_name, _pane_id):
        first_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "a"), None)
        second_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "b"), None)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [new_name], first_env) as first:
                assert is_background_tasks_supervisor_polling(first)
                with running_background_tasks_supervisor(script_path, [new_name], second_env) as second:
                    assert is_background_tasks_supervisor_polling(second)


def _window_names(session_name: str) -> list[str]:
    return subprocess.run(
        ["tmux", "list-windows", "-t", f"={session_name}", "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


@pytest.mark.tmux
def test_an_app_server_spawn_stored_before_a_rename_starts_the_window_once_upgraded() -> None:
    """A renamed codex agent's stored command spawns its app-server window into the session's old name, which
    no longer exists, leaving the TUI no daemon; upgrading the stored command makes the spawn find the session.
    """
    with renamed_tmux_session() as (old_name, new_name, pane_id):
        stored_spawn = (
            f"tmux new-window -d -t {shlex.quote(old_name)} -n codex-app-server {shlex.quote('sleep 48219')}"
        )
        pane_env = {**os.environ, "TMUX_PANE": pane_id}

        stale_result = subprocess.run(["bash", "-c", stored_spawn], env=pane_env, capture_output=True, text=True)
        assert stale_result.returncode != 0
        assert "codex-app-server" not in _window_names(new_name)

        subprocess.run(["bash", "-c", upgrade_app_server_window_spawn(stored_spawn)], env=pane_env, check=True)
        assert "codex-app-server" in _window_names(new_name)
