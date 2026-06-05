"""Integration tests for the tutor step checks against a real created agent.

``checks_test.py`` covers the fast "agent/session not found" branches as unit tests.
These exercise the *positive* branches -- the substantive logic in ``checks.py`` that
the unit tests never reach (state matching, work-dir path joining, the tmux
``list-clients`` parsing) -- which require a real agent and therefore tmux.
"""

from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr_tutor.checks import run_check
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck


def _get_agent_details(mngr_ctx: MngrContext, agent_name: AgentName) -> AgentDetails:
    result = list_agents(mngr_ctx=mngr_ctx, is_streaming=False)
    for agent in result.agents:
        if agent.name == agent_name:
            return agent
    raise AssertionError(f"Expected agent {agent_name!r} to be listed, found: {[a.name for a in result.agents]}")


@pytest.mark.tmux
def test_agent_exists_checks_reflect_a_real_created_agent(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """AgentExistsCheck is True (and AgentNotExistsCheck False) once the agent really exists.

    The unit tests only cover the missing-agent branch, so a bug that made
    _check_agent_exists always return False would slip past them but fail here.
    """
    agent_name = f"tutor-exists-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_git_repo, mngr_test_prefix, plugin_manager, agent_name, command="sleep 748213"
        )

        assert run_check(AgentExistsCheck(agent_name=AgentName(agent_name)), temp_mngr_ctx) is True
        assert run_check(AgentNotExistsCheck(agent_name=AgentName(agent_name)), temp_mngr_ctx) is False


@pytest.mark.tmux
def test_agent_in_state_check_matches_the_agents_actual_state(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """AgentInStateCheck is True only when the agent's real state is in expected_states.

    Covers the `agent.state in expected_states` comparison in _check_agent_in_state,
    which the unit tests (missing agent -> False) never reach.
    """
    agent_name = f"tutor-state-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_git_repo, mngr_test_prefix, plugin_manager, agent_name, command="sleep 837214"
        )

        actual_state = _get_agent_details(temp_mngr_ctx, AgentName(agent_name)).state
        non_matching_state = (
            AgentLifecycleState.STOPPED if actual_state != AgentLifecycleState.STOPPED else AgentLifecycleState.DONE
        )

        matching_check = AgentInStateCheck(agent_name=AgentName(agent_name), expected_states=(actual_state,))
        non_matching_check = AgentInStateCheck(agent_name=AgentName(agent_name), expected_states=(non_matching_state,))

        assert run_check(matching_check, temp_mngr_ctx) is True
        assert run_check(non_matching_check, temp_mngr_ctx) is False


@pytest.mark.tmux
def test_file_exists_in_work_dir_check_detects_files_under_the_agents_work_dir(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """FileExistsInAgentWorkDirCheck resolves the path relative to the agent's work_dir.

    Covers the `(agent.work_dir / file_path).exists()` logic in
    _check_file_exists_in_work_dir, including the False case for an absent file.
    """
    agent_name = f"tutor-file-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_git_repo, mngr_test_prefix, plugin_manager, agent_name, command="sleep 926351"
        )

        work_dir = _get_agent_details(temp_mngr_ctx, AgentName(agent_name)).work_dir
        marker_name = f"tutor-marker-{uuid4().hex}.txt"
        (work_dir / marker_name).write_text("present")

        present_check = FileExistsInAgentWorkDirCheck(agent_name=AgentName(agent_name), file_path=marker_name)
        absent_check = FileExistsInAgentWorkDirCheck(
            agent_name=AgentName(agent_name), file_path=f"absent-{uuid4().hex}.txt"
        )

        assert run_check(present_check, temp_mngr_ctx) is True
        assert run_check(absent_check, temp_mngr_ctx) is False


@pytest.mark.tmux
def test_tmux_session_has_clients_check_is_false_when_session_has_no_attached_client(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """With a live session but no attached client, the check returns False.

    This reaches the `list-clients` succeeds (returncode 0) but empty-output branch of
    _check_tmux_session_has_clients, distinct from the no-session (returncode != 0) case
    in checks_test.py. The fully-positive path (a client attached) is not covered here
    because attaching a real tmux client requires a controlling terminal.
    """
    agent_name = f"tutor-clients-{uuid4().hex}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_git_repo, mngr_test_prefix, plugin_manager, agent_name, command="sleep 615238"
        )

        check = TmuxSessionHasClientsCheck(agent_name=AgentName(agent_name))

        assert run_check(check, temp_mngr_ctx) is False
