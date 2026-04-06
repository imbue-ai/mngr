"""Discover mngr-managed agents using the mngr Python API."""

from __future__ import annotations

from loguru import logger as _loguru_logger
from pathlib import Path

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger


class AgentInfo(FrozenModel):
    """Lightweight agent info for the web UI."""

    id: str = Field(description="The agent's unique identifier")
    name: str = Field(description="The agent's human-readable name")
    state: str = Field(description="The agent's lifecycle state (e.g. RUNNING, STOPPED)")
    agent_state_dir: Path = Field(description="Path to the agent's state directory on the local host")
    claude_config_dir: Path = Field(description="Path to the Claude config directory for this agent")


def _get_mngr_context() -> tuple[MngrContext, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="claude-web-chat")
    cg.__enter__()
    try:
        pm = get_or_create_plugin_manager()
        mngr_ctx = load_config(pm, cg, is_interactive=False)
    except BaseException:
        cg.__exit__(None, None, None)
        raise
    return mngr_ctx, cg


def _read_claude_config_dir_from_env_file(agent_state_dir: Path) -> Path:
    """Read CLAUDE_CONFIG_DIR from the agent's env file.

    Each mngr agent has an env file at <agent_state_dir>/env that contains
    key=value pairs. The CLAUDE_CONFIG_DIR is typically set to
    <agent_state_dir>/plugin/claude/anthropic.
    """
    env_file = agent_state_dir / "env"
    if env_file.exists():
        try:
            env_vars = parse_env_file(env_file.read_text())
            if "CLAUDE_CONFIG_DIR" in env_vars:
                return Path(env_vars["CLAUDE_CONFIG_DIR"])
        except OSError:
            logger.debug("Failed to read env file: {}", env_file)
    # Fallback: the conventional location for mngr claude agents
    conventional = agent_state_dir / "plugin" / "claude" / "anthropic"
    if conventional.exists():
        return conventional
    return Path.home() / ".claude"


def discover_agents(
    provider_names: tuple[str, ...] | None = None,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> list[AgentInfo]:
    """List all mngr-managed agents."""
    mngr_ctx, cg = _get_mngr_context()
    try:
        result = list_agents(
            mngr_ctx=mngr_ctx,
            is_streaming=False,
            include_filters=include_filters,
            exclude_filters=exclude_filters,
            provider_names=provider_names,
            error_behavior=ErrorBehavior.CONTINUE,
        )
    finally:
        cg.__exit__(None, None, None)

    # Use default host dir from mngr config for local agents
    default_host_dir = mngr_ctx.config.default_host_dir

    agents: list[AgentInfo] = []
    for agent_details in result.agents:
        agent_id = str(agent_details.id)
        agent_name = str(agent_details.name)
        state = str(agent_details.state.value) if agent_details.state else "unknown"

        # Compute agent state dir from the default host dir
        agent_state_dir = default_host_dir / "agents" / agent_id

        # Get CLAUDE_CONFIG_DIR from the agent's env file
        claude_config_dir = _read_claude_config_dir_from_env_file(agent_state_dir)

        agents.append(
            AgentInfo(
                id=agent_id,
                name=agent_name,
                state=state,
                agent_state_dir=agent_state_dir,
                claude_config_dir=claude_config_dir,
            )
        )

    return agents


def send_message(agent_name: str, message: str) -> bool:
    """Send a message to an agent. Returns True on success."""
    mngr_ctx, cg = _get_mngr_context()
    try:
        result = send_message_to_agents(
            mngr_ctx=mngr_ctx,
            message_content=message,
            include_filters=(f'(name == "{agent_name}" || id == "{agent_name}")',),
            error_behavior=ErrorBehavior.CONTINUE,
        )
    finally:
        cg.__exit__(None, None, None)
    return len(result.successful_agents) > 0
