"""InteractiveTuiAgent: contract for echo-input TUI agents.

This module defines the shape of a TUI agent's interaction with mngr;
the actual implementation of the paste-detection / wait-for-ready /
submit-and-confirm pipeline lives in ``tui_utils``. Subclasses supply the
evidence probes that prove their TUI accepted a submitted message.
"""

from __future__ import annotations

import inspect
import re
from abc import abstractmethod
from typing import Any
from typing import Callable
from typing import ClassVar
from typing import NoReturn
from typing import Sequence
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.agents.base_agent import SendKeysAgent
from imbue.mngr.agents.tui_utils import DEFAULT_CONFIRMATION_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import EnterRetryGate
from imbue.mngr.agents.tui_utils import RELAXED_CONFIRMATION_TIMEOUT_SECONDS
from imbue.mngr.agents.tui_utils import SubmissionConfirmationOutcome
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import UnconfirmedSubmissionReading
from imbue.mngr.agents.tui_utils import is_pane_content_matching_indicator
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr.agents.tui_utils import raise_for_unconfirmed_submission
from imbue.mngr.agents.tui_utils import submit_message_and_confirm
from imbue.mngr.agents.tui_utils import wait_for_tui_ready
from imbue.mngr.errors import InvalidPastePlaceholderPrefixesError
from imbue.mngr.errors import PastedMessageNotShownError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentConfigT


class InteractiveTuiAgent(SendKeysAgent[AgentConfigT]):
    """Base for interactive TUI agents that echo input back to the terminal.

    Subclasses declare:

    * ``TUI_READY_INDICATOR`` -- what the pane shows once the TUI is rendered and
      ready to accept input, on BOTH a fresh start and a resume (and ideally
      stays matching while the TUI is processing). A plain ``str`` (matched as an
      exact substring), a compiled ``re.Pattern`` (matched with ``re.search``),
      or a predicate taking the pane content and returning whether it is ready --
      the type, not the string contents, chooses the matching mode. Reach for a
      ``re.Pattern`` when no single substring captures the ready state, and for a
      predicate when readiness is not a match at all (for example when it depends
      on WHERE in the pane something appears). A harness supplying a predicate
      should return it from ``get_tui_ready_indicator`` rather than storing it in
      the ClassVar, since a function stored on a class binds as a method when read
      through an instance. Polled by ``send_message`` before pasting, and at startup by
      ``wait_for_ready_signal``. A startup-only banner is unsuitable: it does not
      render when resuming a saved session, so prefer the input prompt glyph (or
      the input-box chrome) over a welcome banner.
    * ``PASTE_PLACEHOLDER_PREFIXES`` -- what the input box shows in place of a pasted
      message, which hides the message's own text until it is submitted (for example
      ``[Pasted text #2 +20 lines]``); ``()`` for a TUI that echoes a paste as text.
    * ``_build_enter_retry_gate`` -- the pane test that says whether a swallowed Enter
      may be re-sent: whether the message is still waiting in the input box, which only
      the TUI's own screen can tell apart from a message it already took (shown in the
      scrollback or as a queued entry). ``build_unsubmitted_message_retry_gate`` builds
      the test for a TUI that keeps an accepted message off the input box and in which
      an Enter on an empty input box does nothing.
    * ``_build_submission_evidence_probes`` -- the evidence (transcript records,
      marker files, or the TUI's own screen) that proves the TUI accepted a
      submitted message. The submit-and-confirm engine in ``tui_utils``
      baselines every probe before sending Enter and then polls until one
      confirms; see ``SubmissionEvidenceProbe`` for the confirmation rule.

    Interactive coding TUIs (Claude Code, Antigravity CLI, pi) have complex
    input handlers that can misinterpret Enter as a literal newline when it
    arrives too quickly after the message text, so ``send_message`` waits for
    the paste to render in the pane before submitting, and the engine re-sends
    Enter (never the text) while the pane still shows the message unconsumed.
    """

    TUI_READY_INDICATOR: ClassVar[str | re.Pattern[str] | Callable[[str], bool]]
    PASTE_PLACEHOLDER_PREFIXES: ClassVar[tuple[str, ...]]

    confirmation_timeout_seconds: float = Field(
        default=DEFAULT_CONFIRMATION_TIMEOUT_SECONDS,
        description="How long to poll for submission evidence before an unconfirmed strict send is settled from the screen",
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if not hasattr(cls, "PASTE_PLACEHOLDER_PREFIXES"):
            if inspect.isabstract(cls):
                return
            raise InvalidPastePlaceholderPrefixesError(
                f"{cls.__name__} must declare PASTE_PLACEHOLDER_PREFIXES: what its input box shows in place of a "
                "pasted message, or () when it shows the pasted text"
            )
        prefixes = cls.PASTE_PLACEHOLDER_PREFIXES
        if not isinstance(prefixes, tuple) or not all(isinstance(prefix, str) and prefix != "" for prefix in prefixes):
            raise InvalidPastePlaceholderPrefixesError(
                f"{cls.__name__}.PASTE_PLACEHOLDER_PREFIXES must be a tuple of non-empty strings, got {prefixes!r}"
            )

    def get_tui_ready_indicator(self) -> str | re.Pattern[str] | Callable[[str], bool]:
        return self.TUI_READY_INDICATOR

    @abstractmethod
    def _build_submission_evidence_probes(
        self, message: str, policy: SubmissionConfirmationPolicy
    ) -> Sequence[SubmissionEvidenceProbe]:
        """Return the evidence probes that confirm this message's submission.

        Probes must rely only on artifacts that exist for agents created by
        older mngr versions (raw transcripts, marker files, the agent's screen) -- hook configs are
        frozen at create time, so a probe that needs a newly provisioned hook
        would silently never confirm on an existing agent. An empty sequence
        degrades the send to a best-effort Enter.
        """

    @abstractmethod
    def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
        """Return the pane test that decides whether a swallowed Enter is re-sent for this message.

        None re-sends Enter at every retry offset without reading the pane.
        """

    def _detect_preexisting_input_text(self, pane_content: str) -> str | None:
        """Return leftover input-box text visible in the pane before pasting, if detectable.

        Default: no detection (return None). Subclasses that can recognize
        their input row (e.g. via the prompt glyph) may override; a non-None
        return that ``_clear_preexisting_input_text_if_restored_command`` does
        not clear produces a warning and a structured agent event, and the send
        proceeds, appending to the leftover text.
        """
        return None

    def _clear_preexisting_input_text_if_restored_command(
        self, tmux_target: TmuxWindowTarget | str, leftover_text: str
    ) -> bool:
        """Clear leftover input text that the TUI itself restored there; return whether the row is now empty.

        Some TUIs put a cancelled command back into the input box (Claude Code restores a
        cancelled ``/compact``), and appending the next message to it would submit that message
        as the command's arguments. Default: never clear, so the leftover is warned about and the
        new message is appended. An override must clear only text it recognizes as such a
        restored command, never a draft a person typed, and return True only once the input row
        is confirmed empty.
        """
        return False

    def _capture_input_box_pane_content(self, tmux_target: TmuxWindowTarget | str) -> str | None:
        """Capture the pane for reading what is typed in the input box.

        Default: the plain capture. A TUI that draws text nobody typed in its input box (a
        placeholder hint) overrides this to leave that text out.
        """
        return self._capture_pane_content(tmux_target)

    def _capture_ready_input_box_pane_content(
        self, tmux_target: TmuxWindowTarget | str, preflight_pane_content: str | None
    ) -> str | None:
        """Wait for the TUI to be ready and return the input-box pane content it is ready on.

        Preflight's own reading of the pane answers this when it already shows the ready
        indicator, which spares the send two pane captures; otherwise this polls for readiness
        and captures afresh.
        """
        indicator = self.get_tui_ready_indicator()
        if preflight_pane_content is not None and is_pane_content_matching_indicator(
            preflight_pane_content, indicator
        ):
            return preflight_pane_content
        wait_for_tui_ready(self, tmux_target, indicator)
        return self._capture_input_box_pane_content(tmux_target)

    def _clear_or_warn_about_preexisting_input_text(
        self, tmux_target: TmuxWindowTarget | str, input_box_pane_content: str | None
    ) -> None:
        if input_box_pane_content is None:
            return
        leftover_text = self._detect_preexisting_input_text(input_box_pane_content)
        if leftover_text is None:
            return
        if self._clear_preexisting_input_text_if_restored_command(tmux_target, leftover_text):
            logger.info(
                "Cleared a restored command draft from the input box of agent {} before sending: {!r}",
                self.name,
                leftover_text,
            )
            self.record_message_delivery_event(
                "cleared_restored_input_text",
                f"cleared a restored command draft from the input box before paste: {leftover_text!r}",
            )
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

    def _send_message_holding_lock(self, message: str) -> None:
        """Send a message via paste-detection + evidence-confirmed submission.

        Runs under the message lock (see ``send_message``), which keeps concurrent sends from
        interleaving tmux input -- and, just as importantly, means two mngr sends can never
        confirm against each other's submission evidence. Runs
        ``_preflight_send_message`` first -- errors from preflight indicate a
        condition that won't resolve by resending (e.g., a blocking dialog),
        and a blocking dialog must be surfaced rather than waited on (the ready
        indicator never appears while a dialog occupies the pane). Then waits
        for the TUI to be ready before pasting -- this covers every send path
        (initial message on create, resume message, and any later send), so
        keystrokes are never delivered while a resumed transcript is still
        replaying.

        The agent's pane is looked up once and every step addresses it by that result: each
        lookup and capture is a process spawn, and under gVisor those dominate a send.

        Normal messages are confirmed strictly: if no evidence of acceptance
        appears within ``confirmation_timeout_seconds`` (with bounded,
        pane-gated Enter retries along the way), the agent's screen settles it
        (``_settle_unconfirmed_submission``): shown taken, the send succeeds;
        shown never taken, it raises ``SendMessageError`` as ``NOT_SUBMITTED``;
        otherwise as ``SUBMISSION_UNCONFIRMED``. Slash commands are relaxed: they are TUI-local
        and often leave no observable evidence, so an unconfirmed send logs a
        warning and records an agent event instead of failing.
        """
        with log_span("Sending message to agent {} (length={})", self.name, len(message)):
            # A TUI ignores Enter on an empty input box, so a blank message leaves no evidence
            # and would otherwise poll out the whole confirmation window before failing.
            if message.strip() == "":
                raise SendMessageError(str(self.name), "the message is empty")
            target_arg = self._send_target_arg(self.tmux_target)
            preflight_pane_content = self._preflight_send_message(target_arg)
            input_box_pane_content = self._capture_ready_input_box_pane_content(target_arg, preflight_pane_content)
            self._clear_or_warn_about_preexisting_input_text(target_arg, input_box_pane_content)
            self._send_tmux_literal_keys(target_arg, message)

            policy = self._determine_confirmation_policy(message)
            timeout_seconds = (
                RELAXED_CONFIRMATION_TIMEOUT_SECONDS
                if policy == SubmissionConfirmationPolicy.RELAXED
                else self.confirmation_timeout_seconds
            )
            probes = tuple(self._build_submission_evidence_probes(message, policy))
            try:
                outcome = submit_message_and_confirm(
                    agent=self,
                    tmux_target=target_arg,
                    message=message,
                    probes=probes,
                    paste_placeholder_prefixes=self.PASTE_PLACEHOLDER_PREFIXES,
                    enter_retry_gate=self._build_enter_retry_gate(message),
                    timeout_seconds=timeout_seconds,
                )
            except PastedMessageNotShownError as not_shown:
                self._settle_pasted_message_not_shown(target_arg, message, input_box_pane_content, not_shown)
            if outcome.is_confirmed and outcome.is_rejection:
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
            if not outcome.is_confirmed:
                if len(probes) == 0:
                    # No evidence exists for this agent type; the send was a
                    # best-effort Enter and there is nothing to confirm against.
                    logger.debug("Agent type supplies no submission evidence; Enter sent best-effort")
                elif policy == SubmissionConfirmationPolicy.STRICT:
                    self._settle_unconfirmed_submission(
                        target_arg, message, input_box_pane_content, outcome, timeout_seconds
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

            # Reached only when the message was delivered/best-effort-sent (an unconfirmed strict send
            # the screen did not show taken raises above and never gets here): make sure the submitted
            # input did not open a blocking dialog that leaves the agent stuck. Subclasses that can
            # detect such dialogs override this; the default is a no-op.
            self._run_post_submit_dialog_check(target_arg)

    def _settle_unconfirmed_submission(
        self,
        tmux_target: TmuxWindowTarget | str,
        message: str,
        pre_send_pane_content: str | None,
        outcome: SubmissionConfirmationOutcome,
        timeout_seconds: float,
    ) -> None:
        """Settle a strict send no evidence probe confirmed from what the TUI's own screen shows; raise unless taken.

        The probes read what the TUI has recorded, and a TUI can hold a message without recording it
        (claude records nothing until its SessionStart hooks have finished), so a send can outlast the
        window without being in doubt. Only a screen that says neither taken nor not taken leaves the
        send unconfirmed.
        """
        reading = self._read_unconfirmed_submission(tmux_target, message, pre_send_pane_content)
        match reading:
            case UnconfirmedSubmissionReading.TAKEN:
                logger.info(
                    "Agent {} holds the message but had not recorded it within {:.0f}s", self.name, timeout_seconds
                )
                self.record_message_delivery_event(
                    "submission_confirmed_by_screen",
                    f"the screen shows the message taken, unrecorded after {timeout_seconds:.0f}s: {message!r}",
                )
            case UnconfirmedSubmissionReading.NOT_TAKEN:
                raise SendMessageError(
                    str(self.name),
                    "the message never left the agent's input box, so it was cleared from there",
                    SendFailureKind.NOT_SUBMITTED,
                )
            case UnconfirmedSubmissionReading.UNKNOWN:
                raise_for_unconfirmed_submission(
                    agent=self, tmux_target=tmux_target, outcome=outcome, timeout_seconds=timeout_seconds
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _settle_pasted_message_not_shown(
        self,
        tmux_target: TmuxWindowTarget | str,
        message: str,
        pre_send_pane_content: str | None,
        not_shown: PastedMessageNotShownError,
    ) -> NoReturn:
        """Raise for a typed message that never showed in the input box, as ``NOT_SUBMITTED`` once the screen clears it.

        Enter was never sent, so nothing was submitted; what is left to settle is text that reached
        the input box late and would have the next message appended to it.
        """
        reading = self._read_unconfirmed_submission(tmux_target, message, pre_send_pane_content)
        if reading is UnconfirmedSubmissionReading.NOT_TAKEN:
            raise SendMessageError(
                str(self.name),
                f"{not_shown.reason}; the part that reached the input box was cleared from there",
                SendFailureKind.NOT_SUBMITTED,
            ) from not_shown
        raise not_shown

    def _read_unconfirmed_submission(
        self, tmux_target: TmuxWindowTarget | str, message: str, pre_send_pane_content: str | None
    ) -> UnconfirmedSubmissionReading:
        """Read the screen for a send that is not confirmed (see ``UnconfirmedSubmissionReading``).

        Called for a strict send no evidence probe confirmed, and, under any policy, for a typed
        message that had not shown in the input box, so Enter was never sent.

        ``pre_send_pane_content`` is the input-box capture the send started from, before the message
        was typed (None when there was none): what was already on screen, which an override must
        not read as this send. Default: UNKNOWN. An override that returns NOT_TAKEN must first have
        cleared the message from the input box: the send then fails as ``NOT_SUBMITTED``, which
        tells the sender that sending it again delivers it once.
        """
        return UnconfirmedSubmissionReading.UNKNOWN

    def _determine_confirmation_policy(self, message: str) -> SubmissionConfirmationPolicy:
        """Choose the confirmation policy for a message.

        Slash commands are TUI-local and often leave no durable evidence, so
        they are confirmed under the relaxed policy (an unconfirmed send is
        logged, not raised). Everything else is strict: a normal message must
        never silently vanish. Subclasses may widen the relaxed set for other
        TUI-local inputs they understand.
        """
        if is_slash_command_message(message):
            return SubmissionConfirmationPolicy.RELAXED
        return SubmissionConfirmationPolicy.STRICT

    def _run_post_submit_dialog_check(self, tmux_target: TmuxWindowTarget | str) -> None:
        """Handle any blocking dialog opened by the just-submitted message.

        Called after a send has been delivered/confirmed. Default is a no-op.
        Subclasses (e.g. Claude) detect an interactive selector the submitted
        input may have opened and either auto-accept it or raise
        ``MessageDeliveredButBlockedError`` so the caller learns the agent is
        blocked even though the message itself landed.
        """

    def wait_for_ready_signal(
        self, is_readiness_awaited: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Run the start action; when ``is_readiness_awaited``, also wait for the TUI ready indicator.

        Callers pass ``is_readiness_awaited=True`` when creating an agent and ``False`` when
        starting/resuming one. ``send_message`` independently waits for readiness, so this wait only
        matters for agents created without an initial message (where ``send_message`` is never
        called). When a message follows, the readiness check in ``send_message`` is a no-op because
        the indicator is already present, so there is no awkward double-wait.
        """
        super().wait_for_ready_signal(is_readiness_awaited, start_action, timeout)
        if is_readiness_awaited:
            wait_for_tui_ready(self, self.tmux_target, self.get_tui_ready_indicator())
