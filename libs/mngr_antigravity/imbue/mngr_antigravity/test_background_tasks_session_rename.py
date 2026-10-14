"""antigravity_background_tasks.sh keeps supervising after the agent's session is renamed.

Renaming an agent renames its tmux session, so the session name a supervisor was
launched with goes stale. The supervisor therefore identifies its session through the
pane it runs in (``$TMUX_PANE``) and keys its pidfile on the agent id.
"""

import importlib.resources
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import is_background_tasks_supervisor_polling
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_antigravity import resources as _antigravity_resources


@pytest.mark.tmux
def test_antigravity_background_tasks_keeps_polling_when_launched_with_the_pre_rename_session_name(
    tmp_path: Path,
) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    script = importlib.resources.files(_antigravity_resources) / "antigravity_background_tasks.sh"
    with renamed_tmux_session() as (old_name, _new_name, pane_id):
        env = build_background_tasks_supervisor_env(state_dir, pane_id)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [old_name], env) as process:
                assert is_background_tasks_supervisor_polling(process)

    events_log = state_dir / "events" / "logs" / "antigravity_background_tasks" / "events.jsonl"
    assert "(session ended)" not in events_log.read_text()


@pytest.mark.tmux
def test_antigravity_background_tasks_for_different_agents_sharing_a_session_name_both_run(
    tmp_path: Path,
) -> None:
    script = importlib.resources.files(_antigravity_resources) / "antigravity_background_tasks.sh"
    with renamed_tmux_session() as (_old_name, new_name, _pane_id):
        first_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "a"), None)
        second_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "b"), None)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [new_name], first_env) as first:
                assert is_background_tasks_supervisor_polling(first)
                with running_background_tasks_supervisor(script_path, [new_name], second_env) as second:
                    assert is_background_tasks_supervisor_polling(second)
