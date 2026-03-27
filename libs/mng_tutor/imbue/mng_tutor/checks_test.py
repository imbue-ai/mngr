import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng_tutor.checks import run_check
from imbue.mng_tutor.data_types import AgentExistsCheck
from imbue.mng_tutor.data_types import AgentInStateCheck
from imbue.mng_tutor.data_types import AgentNotExistsCheck
from imbue.mng_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mng_tutor.data_types import TmuxSessionHasClientsCheck


def test_agent_exists_check_returns_false_when_no_agents(temp_mng_ctx: MngContext) -> None:
    check = AgentExistsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mng_ctx) is False


def test_agent_not_exists_check_returns_true_when_no_agents(temp_mng_ctx: MngContext) -> None:
    check = AgentNotExistsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mng_ctx) is True


def test_agent_in_state_check_returns_false_when_no_agents(temp_mng_ctx: MngContext) -> None:
    check = AgentInStateCheck(
        agent_name=AgentName("nonexistent"),
        expected_states=(AgentLifecycleState.RUNNING,),
    )
    assert run_check(check, temp_mng_ctx) is False


def test_file_exists_check_returns_false_when_no_agents(temp_mng_ctx: MngContext) -> None:
    check = FileExistsInAgentWorkDirCheck(
        agent_name=AgentName("nonexistent"),
        file_path="hello.txt",
    )
    assert run_check(check, temp_mng_ctx) is False


@pytest.mark.tmux
def test_tmux_session_has_clients_check_returns_false_when_no_session(temp_mng_ctx: MngContext) -> None:
    check = TmuxSessionHasClientsCheck(agent_name=AgentName("nonexistent"))
    assert run_check(check, temp_mng_ctx) is False
