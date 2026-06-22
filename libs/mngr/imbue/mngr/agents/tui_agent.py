"""InteractiveTuiAgent: contract for echo-input TUI agents.

This module defines the shape of a TUI agent's interaction with mngr;
the actual implementation of the paste-detection / wait-for-ready / send-Enter
pipeline lives in ``tui_utils``. Subclasses pick a send-Enter strategy by
calling the relevant helper from ``_send_enter_and_validate``.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from typing import Callable
from typing import ClassVar

from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.tui_utils import DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import wait_for_paste_visible
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentConfigT


class InteractiveTuiAgent(SendKeysAgent[AgentConfigT]):
    """Base for interactive TUI agents that echo input back to the terminal.

    Subclasses declare:

    * ``TUI_READY_INDICATOR`` -- what the pane shows once the TUI is rendered and
      ready to accept input, on BOTH a fresh start and a resume (and ideally
      stays matching while the TUI is processing). Either a plain ``str``
      (matched as an exact substring) or a compiled ``re.Pattern`` (matched with
      ``re.search``) -- the type, not the string contents, chooses the matching
      mode, so reach for a ``re.Pattern`` when no single substring captures the
      ready state. Polled by ``send_message`` before pasting, and at startup by
      ``wait_for_ready_signal``. A startup-only banner is unsuitable: it does not
      render when resuming a saved session, so prefer the input prompt glyph (or
      the input-box chrome) over a welcome banner.
    * ``_send_enter_and_validate`` -- how to submit a message and confirm it
      landed. Pick one of the strategies in ``tui_utils``:
      ``send_enter_via_tmux_wait_for_hook`` (for agents whose TUI fires a
      UserPromptSubmit-style hook into a tmux wait-for channel),
      ``send_enter_and_poll_for_cleared_indicator`` (for agents with a
      dynamic input-row placeholder that disappears during typing and
      reappears after submission), or ``send_enter_best_effort`` (for agents
      with no reliable confirmation surface).

    Interactive coding TUIs (Claude Code, Antigravity CLI, pi) have complex
    input handlers that can misinterpret Enter as a literal newline when it
    arrives too quickly after the message text, so ``send_message`` waits for
    the paste to render in the pane before invoking ``_send_enter_and_validate``.
    """

    TUI_READY_INDICATOR: ClassVar[str | re.Pattern[str]]

    enter_submission_timeout_seconds: float = Field(
        default=DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS,
        description="Timeout in seconds for the signal-based submission strategy",
    )

    def get_tui_ready_indicator(self) -> str | re.Pattern[str]:
        return self.TUI_READY_INDICATOR

    @abstractmethod
    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        """Send Enter to submit the pasted message, then confirm submission.

        Implementations should call one of the strategy helpers in
        ``imbue.mngr.agents.tui_utils`` and raise ``SendMessageError`` on
        failure.
        """

    def send_message(self, message: str) -> None:
        """Send a message via paste-detection + the subclass's Enter strategy.

        Acquires an exclusive file lock to prevent concurrent sends from
        interleaving tmux input. Runs ``_preflight_send_message`` first --
        errors from preflight indicate a condition that won't resolve by
        resending (e.g., a blocking dialog), and a blocking dialog must be
        surfaced rather than waited on (the ready indicator never appears while
        a dialog occupies the pane). Then waits for the TUI to be ready before
        pasting -- this covers every send path (initial message on create,
        resume message, and any later send), so keystrokes are never delivered
        while a resumed transcript is still replaying.
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)
            wait_for_tui_ready(self, self.tmux_target, self.get_tui_ready_indicator())
            self._send_tmux_literal_keys(self.tmux_target, message)
            wait_for_paste_visible(self, self.tmux_target, message)
            self._send_enter_and_validate(self.tmux_target)

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Run the start action; on creation, also wait for the TUI ready indicator.

        ``send_message`` independently waits for readiness, so this create-path
        wait only matters for agents created without an initial message (where
        ``send_message`` is never called). When a message follows, the readiness
        check in ``send_message`` is a no-op because the indicator is already
        present, so there is no awkward double-wait.
        """
        super().wait_for_ready_signal(is_creating, start_action, timeout)
        if is_creating:
            wait_for_tui_ready(self, self.tmux_target, self.get_tui_ready_indicator())
