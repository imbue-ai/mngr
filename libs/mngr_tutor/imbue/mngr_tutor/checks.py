import subprocess
from typing import assert_never

from loguru import logger

from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import StepCheck
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck


def _find_agent_by_name(agent_name: AgentName, mngr_ctx: MngrContext) -> AgentDetails | None:
    """Find an agent by name, returning None if not found."""
    result = list_agents(mngr_ctx, is_streaming=False, error_behavior=ErrorBehavior.CONTINUE)
    for agent in result.agents:
        if agent.name == agent_name:
            return agent
    return None


def _check_agent_exists(agent_name: AgentName, mngr_ctx: MngrContext) -> bool:
    """Check if an agent with the given name exists."""
    return _find_agent_by_name(agent_name, mngr_ctx) is not None


def _check_agent_in_state(
    agent_name: AgentName,
    expected_states: tuple[AgentLifecycleState, ...],
    mngr_ctx: MngrContext,
) -> bool:
    """Check if an agent is in one of the expected lifecycle states."""
    agent = _find_agent_by_name(agent_name, mngr_ctx)
    if agent is None:
        return False
    return agent.state in expected_states


def _check_file_exists_in_work_dir(
    agent_name: AgentName,
    file_path: str,
    mngr_ctx: MngrContext,
) -> bool:
    """Check if a file exists in the agent's working directory."""
    agent = _find_agent_by_name(agent_name, mngr_ctx)
    if agent is None:
        return False
    full_path = agent.work_dir / file_path
    return full_path.exists()


def _check_tmux_session_has_clients(agent_name: AgentName, mngr_ctx: MngrContext) -> bool:
    """Check if the agent's tmux session has at least one attached client."""
    session_name = f"{mngr_ctx.config.prefix}{agent_name}"
    result = subprocess.run(
        ["tmux", "list-clients", "-t", session_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return len(result.stdout.strip()) > 0


def _execute_check(check: StepCheck, mngr_ctx: MngrContext) -> bool:
    """Execute the check logic for a single step."""
    if isinstance(check, AgentExistsCheck):
        return _check_agent_exists(check.agent_name, mngr_ctx)
    elif isinstance(check, AgentNotExistsCheck):
        return not _check_agent_exists(check.agent_name, mngr_ctx)
    elif isinstance(check, AgentInStateCheck):
        return _check_agent_in_state(check.agent_name, check.expected_states, mngr_ctx)
    elif isinstance(check, FileExistsInAgentWorkDirCheck):
        return _check_file_exists_in_work_dir(check.agent_name, check.file_path, mngr_ctx)
    elif isinstance(check, TmuxSessionHasClientsCheck):
        return _check_tmux_session_has_clients(check.agent_name, mngr_ctx)
    else:
        assert_never(check)


def run_check(check: StepCheck, mngr_ctx: MngrContext) -> bool:
    """Execute a step check and return whether it passes."""
    try:
        return _execute_check(check, mngr_ctx)
    except (BaseMngrError, OSError):
        logger.debug("Check failed with exception for check type: {}", type(check).__name__)
        return False
