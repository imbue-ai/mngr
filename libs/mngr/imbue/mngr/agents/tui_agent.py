from __future__ import annotations

from typing import ClassVar

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.interfaces.agent import AgentConfigT


class InteractiveTuiAgent(BaseAgent[AgentConfigT]):
    """BaseAgent for interactive TUI agents that echo input back to the terminal.

    Subclasses set ``TUI_READY_INDICATOR`` to a stable substring that appears in
    the pane content once the TUI has finished initializing and is ready to
    accept input. Paste-detection send is always enabled so messages are not
    submitted before the pasted content has been confirmed visible on screen.
    """

    TUI_READY_INDICATOR: ClassVar[str]

    def get_tui_ready_indicator(self) -> str | None:
        return self.TUI_READY_INDICATOR

    def uses_paste_detection_send(self) -> bool:
        return True
