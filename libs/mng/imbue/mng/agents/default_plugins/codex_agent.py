from __future__ import annotations

from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.primitives import CommandString


class CodexAgentConfig(AgentTypeConfig):
    """Config for the codex agent type."""

    command: CommandString = Field(
        default=CommandString("codex"),
        description="Command to run codex agent",
    )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the codex agent type."""
    return ("codex", None, CodexAgentConfig)
