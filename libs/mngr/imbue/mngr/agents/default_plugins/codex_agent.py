from __future__ import annotations

from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import CommandString


class CodexAgentConfig(AgentTypeConfig):
    """Config for the codex agent type."""

    command: CommandString = Field(
        default=CommandString("codex"),
        description="Command to run codex agent",
    )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the codex agent type.

    Uses ``BaseAgent`` directly: ``CodexAgentConfig.command`` defaults to
    ``codex``, so ``BaseAgent.assemble_command`` produces ``codex`` plus any
    ``cli_args`` / ``agent_args`` appended on top.
    """
    return ("codex", BaseAgent, CodexAgentConfig)
