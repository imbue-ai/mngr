from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import CommandString


class OpenCodeAgentConfig(AgentTypeConfig):
    """Config for the opencode agent type."""

    command: CommandString = Field(
        default=CommandString("opencode"),
        description="Command to run opencode agent",
    )


# Module-level hook implementation for pluggy entry point discovery
@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the opencode agent type.

    Uses ``BaseAgent`` directly: ``OpenCodeAgentConfig.command`` defaults to
    ``opencode``, so ``BaseAgent.assemble_command`` produces ``opencode`` plus
    any ``cli_args`` / ``agent_args`` appended on top.
    """
    return ("opencode", BaseAgent, OpenCodeAgentConfig)
