"""InteractiveTuiAgent: contract for echo-input TUI agents.

This module defines the shape of a TUI agent's interaction with mngr;
the actual implementation of the paste-detection / wait-for-ready /
submit-and-confirm pipeline lives in ``tui_utils``. Subclasses supply the
durable evidence probes that prove their TUI accepted a submitted message.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from typing import Callable
from typing import ClassVar
from typing import Sequence

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.tui_utils import DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import RELAXED_CONFIRMATION_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr.agents.tui_utils import raise_for_unconfirmed_submission
from imbue.mngr.agents.tui_utils import submit_message_and_confirm
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
    * ``_build_submission_evidence_probes`` -- the durable on-disk evidence
      (transcript records, marker files) that proves the TUI accepted a
      submitted message. The submit-and-confirm engine in ``tui_utils``
      baselines every probe before sending Enter and then polls until one
      confirms; see ``SubmissionEvidenceProbe`` for the confirmation rule.

    Interactive coding TUIs (Claude Code, Antigravity CLI, pi) have complex
    input handlers that can misinterpret Enter as a literal newline when it
    arrives too quickly after the message text, so ``send_message`` waits for
    the paste to render in the pane before submitting, and the engine re-sends
    Enter (never the text) while the pane still shows the message unconsumed.
    """

    TUI_READY_INDICATOR: ClassVar[str | re.Pattern[str]]

    confirmation_timeout_seconds: float = Field(
        default=DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
        description="How long to poll for durable submission evidence before a strict send fails",
    )

    def get_tui_ready_indicator(self) -> str | re.Pattern[str]:
        return self.TUI_READY_INDICATOR

    @abstractmethod
    def _build_submission_evidence_probes(
        self, message: str, policy: SubmissionConfirmationPolicy
    ) -> Sequence[SubmissionEvidenceProbe]:
        """Return the durable-evidence probes that confirm this message's submission.

        Probes must rely only on artifacts that exist for agents created by
        older mngr versions (raw transcripts, marker files) -- hook configs are
        frozen at create time, so a probe that needs a newly provisioned hook
        would silently never confirm on an existing agent. An empty sequence
        degrades the send to a best-effort Enter.
        """

    def _detect_preexisting_input_text(self, pane_content: str) -> str | None:
        """Return leftover input-box text visible in the pane before pasting, if detectable.

        Default: no detection (return None). Subclasses that can recognize
        their input row (e.g. via the prompt glyph) may override; a non-None
        return produces a warning and a structured agent event, and the send
        proceeds (today's append behavior is preserved).
        """
        return None

    def _warn_if_preexisting_input_text(self, tmux_target: TmuxWindowTarget) -> None:
        pane_content = self._capture_pane_content(tmux_target)
        if pane_content is None:
            return
        leftover_text = self._detect_preexisting_input_text(pane_content)
        if leftover_text is None:
            return
        logger.warning(
            "Input box of agent {} already contains text before sending; the new message will be appended: {!r}",
            self.name,
            leftover_text,
        )
        self.record_message_delivery_event(
            "preexisting_input_text",
            f"input box already contained text before paste: {leftover_text!r}",
        )

    def send_message(self, message: str) -> None:
        """Send a message via paste-detection + evidence-confirmed submission.

        Acquires an exclusive file lock to prevent concurrent sends from
        interleaving tmux input -- and, just as importantly, so two mngr sends
        can never confirm against each other's submission evidence. Runs
        ``_preflight_send_message`` first -- errors from preflight indicate a
        condition that won't resolve by resending (e.g., a blocking dialog),
        and a blocking dialog must be surfaced rather than waited on (the ready
        indicator never appears while a dialog occupies the pane). Then waits
        for the TUI to be ready before pasting -- this covers every send path
        (initial message on create, resume message, and any later send), so
        keystrokes are never delivered while a resumed transcript is still
        replaying.

        Normal messages are confirmed strictly: if no durable evidence of
        acceptance appears within ``confirmation_timeout_seconds`` (with
        bounded, pane-gated Enter retries along the way), this raises
        ``SendMessageError``. Slash commands are relaxed: they are TUI-local
        and often leave no observable evidence, so an unconfirmed send logs a
        warning and records an agent event instead of failing.
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)
            wait_for_tui_ready(self, self.tmux_target, self.get_tui_ready_indicator())
            self._warn_if_preexisting_input_text(self.tmux_target)
            self._send_tmux_literal_keys(self.tmux_target, message)
            wait_for_paste_visible(self, self.tmux_target, message)

            policy = (
                SubmissionConfirmationPolicy.RELAXED
                if is_slash_command_message(message)
                else SubmissionConfirmationPolicy.STRICT
            )
            timeout_seconds = (
                RELAXED_CONFIRMATION_TIMEOUT_SECONDS
                if policy == SubmissionConfirmationPolicy.RELAXED
                else self.confirmation_timeout_seconds
            )
            probes = tuple(self._build_submission_evidence_probes(message, policy))
            outcome = submit_message_and_confirm(
                agent=self,
                tmux_target=self.tmux_target,
                message=message,
                probes=probes,
                timeout_seconds=timeout_seconds,
            )
            if outcome.is_confirmed:
                if outcome.is_rejection:
                    logger.warning(
                        "Agent {} rejected {!r}; nothing was executed (evidence: {})",
                        self.name,
                        message,
                        outcome.confirming_probe_name,
                    )
                    self.record_message_delivery_event(
                        "send_rejected_by_agent",
                        f"agent rejected the message {message!r} (evidence: {outcome.confirming_probe_name})",
                    )
                return
            if len(probes) == 0:
                # No evidence exists for this agent type; the send was a
                # best-effort Enter and there is nothing to confirm against.
                logger.debug("Agent type supplies no submission evidence; Enter sent best-effort")
                return
            if policy == SubmissionConfirmationPolicy.STRICT:
                raise_for_unconfirmed_submission(
                    agent=self,
                    tmux_target=self.tmux_target,
                    outcome=outcome,
                    timeout_seconds=timeout_seconds,
                )
            else:
                logger.warning(
                    "Sent {!r} to agent {} but observed no submission evidence within {:.0f}s; "
                    "slash commands are best-effort, so the send is reported as successful",
                    message,
                    self.name,
                    timeout_seconds,
                )
                self.record_message_delivery_event(
                    "relaxed_send_unconfirmed",
                    f"no submission evidence within {timeout_seconds:.0f}s for message: {message!r}",
                )

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
