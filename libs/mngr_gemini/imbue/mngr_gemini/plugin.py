from __future__ import annotations

from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.primitives import CommandString


class GeminiAgentConfig(AgentTypeConfig):
    """Config for the gemini agent type."""

    command: CommandString = Field(
        default=CommandString("gemini"),
        description="Command to run gemini agent",
    )


class GeminiAgent(InteractiveTuiAgent[GeminiAgentConfig]):
    """Agent implementation for Google's Gemini CLI."""

    # Substring that appears in the gemini TUI's input row once the prompt is
    # fully rendered and ready to accept input. Discovered empirically by
    # running `gemini` in a probe tmux session and capturing the pane.
    TUI_READY_INDICATOR = "Type your message"


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
