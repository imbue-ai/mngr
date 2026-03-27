import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr_tutor.checks import run_check
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck


def test_agent_exists_check_returns_false_when_no_agents(temp_mngr_ctx: MngrContext) -> None:
    check = AgentExistsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mngr_ctx) is False


def test_agent_not_exists_check_returns_true_when_no_agents(temp_mngr_ctx: MngrContext) -> None:
    check = AgentNotExistsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mngr_ctx) is True


def test_agent_in_state_check_returns_false_when_no_agents(temp_mngr_ctx: MngrContext) -> None:
    check = AgentInStateCheck(
        agent_name=AgentName("nonexistent"),
        expected_states=(AgentLifecycleState.RUNNING,),
    )
    assert run_check(check, temp_mngr_ctx) is False


def test_file_exists_check_returns_false_when_no_agents(temp_mngr_ctx: MngrContext) -> None:
    check = FileExistsInAgentWorkDirCheck(
        agent_name=AgentName("nonexistent"),
        file_path="hello.txt",
    )
    assert run_check(check, temp_mngr_ctx) is False


@pytest.mark.tmux
def test_tmux_session_has_clients_check_returns_false_when_no_session(temp_mngr_ctx: MngrContext) -> None:
    check = TmuxSessionHasClientsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mngr_ctx) is False
