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
    # `--skip-trust` bypasses gemini's first-run "Do you trust this folder?"
    # dialog by trusting the workspace for this session only. Without it the
    # agent reaches the TUI but cannot accept input until a human picks an
    # option. Placed in the default cli_args (rather than hardcoded into
    # assemble_command) so a user-defined parent_type can append additional
    # flags via the normal merge_with concatenation.
    cli_args: tuple[str, ...] = Field(
        default=("--skip-trust",),
        description="Additional CLI arguments to pass to the gemini agent",
    )


class GeminiAgent(InteractiveTuiAgent[GeminiAgentConfig]):
    """Agent implementation for Google's Gemini CLI."""

    # Substring that appears in the gemini TUI's input row once the prompt is
    # fully rendered and ready to accept input. Discovered empirically by
    # running `gemini` in a probe tmux session and capturing the pane. Also
    # used by the no-submission-signal Enter path to detect when the input
    # has cleared after submitting.
    TUI_READY_INDICATOR = "Type your message"

    def get_expected_process_name(self) -> str:
        # `gemini` is a `#!/usr/bin/env node` script and (unlike `claude`) does
        # not override `process.title`, so the running process shows up as
        # `node` in ps/tmux. Report that so lifecycle detection finds it.
        return "node"

    def uses_submission_signal(self) -> bool:
        # Gemini's CLI has no equivalent of claude's UserPromptSubmit hook to
        # signal a tmux wait-for channel; InteractiveTuiAgent falls back to
        # polling the TUI_READY_INDICATOR for input-prompt clear instead.
        return False


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the gemini agent type."""
    return ("gemini", GeminiAgent, GeminiAgentConfig)
