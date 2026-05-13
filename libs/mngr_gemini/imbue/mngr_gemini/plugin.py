from __future__ import annotations

from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import CommandString


class GeminiAgentConfig(AgentTypeConfig):
    """Config for the gemini agent type."""

    command: CommandString = Field(
        default=CommandString("gemini"),
        description="Command to run gemini agent",
    )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", None, GeminiAgentConfig)
