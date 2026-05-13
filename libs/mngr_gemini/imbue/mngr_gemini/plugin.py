from __future__ import annotations

from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
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

    def get_expected_process_name(self) -> str:
        # `gemini` is a `#!/usr/bin/env node` script and (unlike `claude`) does
        # not override `process.title`, so the running process shows up as
        # `node` in ps/tmux. Report that so lifecycle detection finds it.
        return "node"

    def uses_submission_signal(self) -> bool:
        # Gemini's CLI has no equivalent of claude's UserPromptSubmit hook to
        # signal a tmux wait-for channel, so we just press Enter and trust the
        # paste-visibility check that already ran upstream.
        return False

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        # Inject --skip-trust so gemini does not block on the "Do you trust this
        # folder?" first-run dialog. Without it the agent reaches the TUI but
        # cannot accept input until a human picks an option.
        augmented_args = ("--skip-trust", *agent_args)
        return super().assemble_command(host, augmented_args, command_override, initial_message)


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
