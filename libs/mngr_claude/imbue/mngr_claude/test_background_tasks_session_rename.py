"""claude_background_tasks.sh keeps supervising after the agent's session is renamed.

Renaming an agent renames its tmux session, so the session name a supervisor was
launched with goes stale. The supervisor therefore identifies its session through the
pane it runs in (``$TMUX_PANE``) and keys its pidfile on the agent id. An agent
created by an older mngr runs that mngr's supervisor, which trusts the name, so start
upgrades its stored launch command to pass the session's current name.
"""

import importlib.resources
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.tmux import PANE_SESSION_NAME_SHELL_EXPRESSION
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import is_background_tasks_supervisor_polling
from imbue.mngr.utils.testing import renamed_tmux_session
from imbue.mngr.utils.testing import running_background_tasks_supervisor
from imbue.mngr_claude import resources as _claude_resources
from imbue.mngr_claude.plugin import ClaudeAgent


@pytest.mark.tmux
def test_claude_background_tasks_keeps_polling_when_launched_with_the_pre_rename_session_name(
    tmp_path: Path,
) -> None:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    script = importlib.resources.files(_claude_resources) / "claude_background_tasks.sh"
    with renamed_tmux_session() as (old_name, _new_name, pane_id):
        env = build_background_tasks_supervisor_env(state_dir, pane_id)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [old_name], env) as process:
                assert is_background_tasks_supervisor_polling(process)

    events_log = state_dir / "events" / "logs" / "claude_background_tasks" / "events.jsonl"
    assert "(session ended)" not in events_log.read_text()


@pytest.mark.tmux
def test_claude_background_tasks_for_different_agents_sharing_a_session_name_both_run(
    tmp_path: Path,
) -> None:
    script = importlib.resources.files(_claude_resources) / "claude_background_tasks.sh"
    with renamed_tmux_session() as (_old_name, new_name, _pane_id):
        first_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "a"), None)
        second_env = build_background_tasks_supervisor_env(create_background_tasks_state_dir(tmp_path / "b"), None)
        with importlib.resources.as_file(script) as script_path:
            with running_background_tasks_supervisor(script_path, [new_name], first_env) as first:
                assert is_background_tasks_supervisor_polling(first)
                with running_background_tasks_supervisor(script_path, [new_name], second_env) as second:
                    assert is_background_tasks_supervisor_polling(second)


def _run_launch_in_pane(launch: str, pane_id: str, state_dir: Path) -> str:
    """Run a stored supervisor launch as the agent's pane would, returning the session argument it passed."""
    env = {**os.environ, "TMUX_PANE": pane_id, "MNGR_AGENT_STATE_DIR": str(state_dir)}
    subprocess.run(["bash", "-c", f"{launch} wait"], env=env, check=True)
    return (state_dir / "received_session").read_text()


@pytest.mark.tmux
def test_a_supervisor_launch_stored_before_a_rename_passes_the_current_session_once_upgraded(tmp_path: Path) -> None:
    """An older mngr's supervisor polls the session it is given, so a stored launch naming the old session ends it."""
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    recorder = commands_dir / "claude_background_tasks.sh"
    recorder.write_text('#!/bin/sh\nprintf %s "$1" > "$MNGR_AGENT_STATE_DIR/received_session"\n')
    recorder.chmod(0o755)
    with renamed_tmux_session() as (old_name, new_name, pane_id):
        stored_launch = (
            f"( $MNGR_AGENT_STATE_DIR/commands/claude_background_tasks.sh {shlex.quote(old_name)} agent ) &"
        )

        assert _run_launch_in_pane(stored_launch, pane_id, tmp_path) == old_name

        upgraded_launch = ClaudeAgent.model_construct().upgrade_stored_command(CommandString(stored_launch))
        assert _run_launch_in_pane(upgraded_launch, pane_id, tmp_path) == new_name


@pytest.mark.tmux
def test_start_upgrades_a_launch_command_an_older_mngr_stored(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    host = local_provider.create_host(HostName(LOCAL_HOST_NAME))
    assert isinstance(host, Host)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = host.create_agent_state(
        work_dir,
        CreateAgentOptions(
            name=AgentName("upgraded-on-start"),
            agent_type=AgentTypeName("claude"),
            command=CommandString("sleep 736251"),
        ),
    )
    data_path = host.host_dir / "agents" / str(agent.id) / "data.json"
    data = json.loads(data_path.read_text())
    current_command = data["command"]
    stored_by_older_mngr = current_command.replace(PANE_SESSION_NAME_SHELL_EXPRESSION, shlex.quote(agent.session_name))
    assert stored_by_older_mngr != current_command
    data["command"] = stored_by_older_mngr
    data_path.write_text(json.dumps(data))

    host.start_agents([agent.id])
    try:
        assert json.loads(data_path.read_text())["command"] == current_command
    finally:
        host.stop_agents([agent.id])
