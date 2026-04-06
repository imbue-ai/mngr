"""Discover mngr-managed agents using the mngr Python API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager


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


def discover_agents() -> list[AgentInfo]:
    """List all mngr-managed agents."""
    mngr_ctx, cg = _get_mngr_context()
    try:
        result = list_agents(
            mngr_ctx=mngr_ctx,
            is_streaming=False,
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

        # Get CLAUDE_CONFIG_DIR -- check agent's env vars if available,
        # default to ~/.claude
        claude_config_dir = Path.home() / ".claude"
        # If the agent has plugin data with config dir info, use it
        plugin_data: dict[str, Any] = agent_details.plugin or {}
        if "claude" in plugin_data:
            claude_data = plugin_data["claude"]
            if isinstance(claude_data, dict) and "config_dir" in claude_data:
                claude_config_dir = Path(claude_data["config_dir"])

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
