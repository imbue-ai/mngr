from __future__ import annotations

from typing import ClassVar

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentConfigT


class InteractiveTuiAgent(BaseAgent[AgentConfigT]):
    """BaseAgent for interactive TUI agents that echo input back to the terminal.

    Subclasses set ``TUI_READY_INDICATOR`` to a stable substring that appears in
    the pane content once the TUI has finished initializing and is ready to
    accept input. Paste-detection send is always enabled so messages are not
    submitted before the pasted content has been confirmed visible on screen --
    interactive coding TUIs (Claude Code, Gemini CLI) have complex input handlers
    that can misinterpret Enter as a literal newline when it arrives too quickly
    after the message text, so we wait for the paste to render before submitting.
    """

    TUI_READY_INDICATOR: ClassVar[str]

    def get_tui_ready_indicator(self) -> str | None:
        return self.TUI_READY_INDICATOR

    def uses_paste_detection_send(self) -> bool:
        return True

    def uses_submission_signal(self) -> bool:
        """Whether to wait for a tmux wait-for submission signal after pressing Enter.

        Claude's plugin wires this signal via the UserPromptSubmit hook. Agents
        whose CLIs do not expose an equivalent hook should override to return
        False -- they will press Enter and return immediately, relying on the
        prior paste-visibility check to confirm the message reached the TUI.
        """
        return True

    def _send_enter_and_wait(self, tmux_target: str) -> None:
        if self.uses_submission_signal():
            super()._send_enter_and_wait(tmux_target)
            return
        # Brief sleep before Enter so the TUI has time to absorb the pasted
        # text into its input buffer before we submit. Without it, gemini
        # occasionally swallows the Enter on fresh sessions.
        send_enter_cmd = f"sleep 0.2 && tmux send-keys -t '{tmux_target}' Enter"
        result = self.host.execute_stateful_command(send_enter_cmd)
        if not result.success:
            raise SendMessageError(
                str(self.name),
                f"tmux send-keys Enter failed: {result.stderr or result.stdout}",
            )
