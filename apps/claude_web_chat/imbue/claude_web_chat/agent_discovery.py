"""Discover mngr-managed agents using the mngr Python API."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.list import ErrorBehavior
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager

logger = logging.getLogger(__name__)


class AgentInfo(BaseModel, frozen=True):
    """Lightweight agent info for the web UI."""

    id: str
    name: str
    state: str
    agent_state_dir: str
    claude_config_dir: str


def _get_mngr_context() -> tuple[MngrContext, ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="claude-web-chat")
    cg.__enter__()
    pm = get_or_create_plugin_manager()
    mngr_ctx = load_config(pm, cg, is_interactive=False)
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

    agents: list[AgentInfo] = []
    for agent_details in result.agents:
        agent_id = str(agent_details.id)
        agent_name = str(agent_details.name)
        state = str(agent_details.state.value) if agent_details.state else "unknown"

        # Compute agent state dir from the host dir
        host_dir = agent_details.host.host_dir if agent_details.host else None
        if host_dir is not None:
            agent_state_dir = str(Path(host_dir) / "agents" / agent_id)
        else:
            agent_state_dir = ""

        # Get CLAUDE_CONFIG_DIR -- check agent's env vars if available,
        # default to ~/.claude
        claude_config_dir = str(Path.home() / ".claude")
        # If the agent has plugin data with config dir info, use it
        plugin_data: dict[str, Any] = agent_details.plugin or {}
        if "claude" in plugin_data:
            claude_data = plugin_data["claude"]
            if isinstance(claude_data, dict) and "config_dir" in claude_data:
                claude_config_dir = str(claude_data["config_dir"])

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
            include_filters=(agent_name,),
            error_behavior=ErrorBehavior.CONTINUE,
        )
    finally:
        cg.__exit__(None, None, None)
    return len(result.successful_agents) > 0
