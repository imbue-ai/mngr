import subprocess
from typing import assert_never

from loguru import logger

from imbue.mng.api.list import list_agents
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import BaseMngError
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng_tutor.data_types import AgentExistsCheck
from imbue.mng_tutor.data_types import AgentInStateCheck
from imbue.mng_tutor.data_types import AgentNotExistsCheck
from imbue.mng_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mng_tutor.data_types import StepCheck
from imbue.mng_tutor.data_types import TmuxSessionHasClientsCheck


def _find_agent_by_name(agent_name: AgentName, mng_ctx: MngContext) -> AgentInfo | None:
    """Find an agent by name, returning None if not found."""
    result = list_agents(mng_ctx, is_streaming=False, error_behavior=ErrorBehavior.CONTINUE)
    for agent in result.agents:
        if agent.name == agent_name:
            return agent
    return None


def _check_agent_exists(agent_name: AgentName, mng_ctx: MngContext) -> bool:
    """Check if an agent with the given name exists."""
    return _find_agent_by_name(agent_name, mng_ctx) is not None


def _check_agent_in_state(
    agent_name: AgentName,
    expected_states: tuple[AgentLifecycleState, ...],
    mng_ctx: MngContext,
) -> bool:
    """Check if an agent is in one of the expected lifecycle states."""
    agent = _find_agent_by_name(agent_name, mng_ctx)
    if agent is None:
        return False
    return agent.state in expected_states


def _check_file_exists_in_work_dir(
    agent_name: AgentName,
    file_path: str,
    mng_ctx: MngContext,
) -> bool:
    """Check if a file exists in the agent's working directory."""
    agent = _find_agent_by_name(agent_name, mng_ctx)
    if agent is None:
        return False
    full_path = agent.work_dir / file_path
    return full_path.exists()


def _check_tmux_session_has_clients(agent_name: AgentName, mng_ctx: MngContext) -> bool:
    """Check if the agent's tmux session has at least one attached client."""
    session_name = f"{mng_ctx.config.prefix}{agent_name}"
    result = subprocess.run(
        ["tmux", "list-clients", "-t", session_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return len(result.stdout.strip()) > 0


def _execute_check(check: StepCheck, mng_ctx: MngContext) -> bool:
    """Execute the check logic for a single step."""
    if isinstance(check, AgentExistsCheck):
        return _check_agent_exists(check.agent_name, mng_ctx)
    elif isinstance(check, AgentNotExistsCheck):
        return not _check_agent_exists(check.agent_name, mng_ctx)
    elif isinstance(check, AgentInStateCheck):
        return _check_agent_in_state(check.agent_name, check.expected_states, mng_ctx)
    elif isinstance(check, FileExistsInAgentWorkDirCheck):
        return _check_file_exists_in_work_dir(check.agent_name, check.file_path, mng_ctx)
    elif isinstance(check, TmuxSessionHasClientsCheck):
        return _check_tmux_session_has_clients(check.agent_name, mng_ctx)
    else:
        assert_never(check)


def run_check(check: StepCheck, mng_ctx: MngContext) -> bool:
    """Execute a step check and return whether it passes."""
    try:
        return _execute_check(check, mng_ctx)
    except (BaseMngError, OSError):
        logger.debug("Check failed with exception for check type: {}", type(check).__name__)
        return False
