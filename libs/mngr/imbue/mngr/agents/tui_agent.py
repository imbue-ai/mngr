from __future__ import annotations

import re
import shlex
import time
from typing import Callable
from typing import ClassVar
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentConfigT
from imbue.mngr.utils.polling import poll_until

# Constants for the paste-detection + ready-indicator + Enter pipeline.
_SEND_MESSAGE_TIMEOUT_SECONDS: Final[float] = 15.0
# this can take a while, especialy on modal--the process needs to actually
# start and render the TUI before the indicator appears
_TUI_READY_TIMEOUT_SECONDS: Final[float] = 30.0
# Default timeout for the signal-based submission path. Needs to be fairly
# long; can be slow when overloading a host while it is starting (esp Modal).
_DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS: Final[float] = 90.0
# Bounds for the no-submission-signal Enter path. Each attempt sends Enter
# then polls the pane for the TUI ready indicator (input prompt cleared).
# Worst case waits MAX_ATTEMPTS * PER_ATTEMPT_TIMEOUT seconds before raising.
_SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS: Final[int] = 3
_SEND_ENTER_NO_SIGNAL_PER_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 2.0

# Compiled once for _normalize_for_match performance.
_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")


def _normalize_for_match(text: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy matching."""
    return _NON_ALNUM_RE.sub("", text.lower())


def _check_paste_content(pane_content: str, message: str) -> bool:
    """Check whether pasted message content is visible in pane text.

    Returns True if the tmux paste indicator is present OR if a
    normalized tail of the message matches the normalized pane content.
    """
    if "[Pasted text " in pane_content:
        return True

    normalized_pane = _normalize_for_match(pane_content)
    normalized_msg = _normalize_for_match(message)

    probe_length = min(60, len(normalized_msg))
    if probe_length == 0:
        return True
    probe = normalized_msg[-probe_length:]
    return probe in normalized_pane


class InteractiveTuiAgent(BaseAgent[AgentConfigT]):
    """BaseAgent for interactive TUI agents that echo input back to the terminal.

    Owns the full TUI input pipeline: TUI-ready check on startup,
    paste-detection synchronisation on send, and dispatch between a
    hook-driven submission signal (claude) and a poll-based confirmation
    (gemini) after Enter is pressed.

    Two TUI indicators serve distinct purposes:

    * ``TUI_READY_INDICATOR`` -- a *stable* substring that appears in the
      pane once the TUI is rendered and ready to accept input. Polled at
      startup by ``wait_for_ready_signal``. Any persistent banner string
      works (e.g. the agent's name in the header).
    * ``TUI_INPUT_CLEARED_INDICATOR`` -- an *optional, dynamic* substring
      that **disappears** while the user's text is in the input row and
      **reappears** once Enter is consumed and the input clears (typically
      a placeholder string like "Type your message"). Polled after Enter
      in the no-submission-signal path to confirm the submission landed
      and retry on swallowed keystrokes. ``None`` (the default) disables
      post-Enter confirmation -- the no-signal path then degrades to a
      single fire-and-forget Enter.

    Interactive coding TUIs (Claude Code, Gemini CLI, pi) have complex
    input handlers that can misinterpret Enter as a literal newline when it
    arrives too quickly after the message text, so we wait for the paste to
    render before submitting.
    """

    TUI_READY_INDICATOR: ClassVar[str]
    TUI_INPUT_CLEARED_INDICATOR: ClassVar[str | None] = None

    enter_submission_timeout_seconds: float = Field(
        default=_DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS,
        description="Timeout in seconds for waiting on the enter submission signal",
    )

    def get_tui_ready_indicator(self) -> str:
        return self.TUI_READY_INDICATOR

    def get_tui_input_cleared_indicator(self) -> str | None:
        """Return a substring that appears in the pane only when the input row is empty.

        Used by the no-submission-signal Enter path to confirm submission
        and retry on swallowed keystrokes. Return None to disable the poll
        (the no-signal path then degrades to a single best-effort Enter).
        """
        return self.TUI_INPUT_CLEARED_INDICATOR

    def uses_submission_signal(self) -> bool:
        """Whether to wait for a tmux wait-for submission signal after pressing Enter.

        Agents like Claude wire this signal via a UserPromptSubmit hook in
        their TUI; ``_send_enter_and_wait`` arms a tmux wait-for channel that
        the hook fires when the message is submitted. Agents whose CLIs do
        not expose an equivalent hook should override to return False -- the
        Enter path then polls ``get_tui_ready_indicator()`` for the input
        prompt to clear, retrying Enter if the keystroke is swallowed by an
        in-progress paste.
        """
        return True

    def send_message(self, message: str) -> None:
        """Send a message to the running agent using paste-detection synchronisation.

        Acquires an exclusive file lock to prevent concurrent sends from
        interleaving tmux input. Runs preflight checks (e.g., dialog detection)
        first -- errors from preflight indicate a condition that won't resolve
        by resending (e.g., a blocking dialog).
        """
        with self._message_lock(), log_span("Sending message to agent {} (length={})", self.name, len(message)):
            self._preflight_send_message(self.tmux_target)
            self._send_message_with_paste_detection(self.tmux_target, message)

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Run the start action, then wait for the TUI ready indicator on creation.

        On creation the TUI takes time to render its banner -- we poll the
        pane content for ``get_tui_ready_indicator()`` to appear so that
        subsequent input isn't sent to a still-initializing screen.
        """
        super().wait_for_ready_signal(is_creating, start_action, timeout)
        if is_creating:
            self._wait_for_tui_ready(self.tmux_target, self.get_tui_ready_indicator())

    def _send_message_with_paste_detection(self, tmux_target: str, message: str) -> None:
        """Send a message using paste-detection synchronisation.

        Sends the message text WITHOUT a trailing newline, then waits for
        evidence that the text was received before pressing Enter. Evidence
        is either:

        1. The tmux paste indicator (``[Pasted text ``) visible on screen, or
        2. A fuzzy content match: the last chunk of the message (stripped to
           alphanumeric, lowercased) appears in the pane content (similarly
           treated).

        Once the message is confirmed on screen, sends Enter via
        ``_send_enter_and_wait`` for submission synchronisation.
        """
        self._send_tmux_literal_keys(tmux_target, message)
        self._wait_for_paste_visible(tmux_target, message)
        self._send_enter_and_wait(tmux_target)

    def _wait_for_tui_ready(self, tmux_target: str, indicator: str) -> None:
        """Wait until the TUI is ready by looking for the indicator string in the pane.

        This ensures the application's UI is fully rendered before we send input.
        Without this check, input sent too early may be lost or appear as raw text
        instead of being processed by the application's input handler.
        """
        with log_span("Waiting for TUI to be ready (looking for: {})", indicator):
            if not poll_until(
                lambda: self._check_pane_contains(tmux_target, indicator),
                timeout=_TUI_READY_TIMEOUT_SECONDS,
            ):
                pane_content = self._capture_pane_content(tmux_target)
                if pane_content is not None:
                    logger.error(
                        "TUI ready timeout -- remote pane content:\n{}",
                        pane_content,
                    )
                else:
                    logger.error("TUI ready timeout -- failed to capture remote pane content")
                raise SendMessageError(
                    str(self.name),
                    f"Timeout waiting for TUI to be ready (waited {_TUI_READY_TIMEOUT_SECONDS:.1f}s)"
                    + (f"\nPane content:\n{pane_content}" if pane_content else ""),
                )

    def _wait_for_paste_visible(self, tmux_target: str, message: str) -> None:
        """Wait until pasted content is confirmed visible in the tmux pane."""
        with log_span("Waiting for pasted content to appear"):
            if not poll_until(
                lambda: self._is_paste_visible(tmux_target, message),
                timeout=_SEND_MESSAGE_TIMEOUT_SECONDS,
            ):
                self._raise_send_timeout(
                    tmux_target,
                    f"Timeout waiting for pasted content to appear (waited {_SEND_MESSAGE_TIMEOUT_SECONDS:.1f}s)",
                )

    def _is_paste_visible(self, tmux_target: str, message: str) -> bool:
        """Check whether the pasted message is visible in the pane.

        Delegates to the pure ``_check_paste_content`` function after
        capturing the pane. Returns False if the pane cannot be captured.
        """
        content = self._capture_pane_content(tmux_target)
        if content is None:
            return False
        return _check_paste_content(content, message)

    def _send_enter_and_wait(self, tmux_target: str) -> None:
        """Send Enter to submit the message, then wait for confirmation.

        Dispatches based on ``uses_submission_signal()``: when True (the
        default), arms a tmux wait-for channel that the agent's
        UserPromptSubmit-style hook fires; when False, polls the TUI ready
        indicator for the input prompt to clear after Enter is sent.
        """
        if self.uses_submission_signal():
            self._send_enter_and_wait_for_submission_signal(tmux_target)
            return
        self._send_enter_and_poll_for_input_ready(tmux_target)

    def _send_enter_and_wait_for_submission_signal(self, tmux_target: str) -> None:
        """Send Enter and wait for a tmux wait-for channel fired by a TUI hook.

        The wait_channel is derived from the pure session name (without window
        suffix) because the UserPromptSubmit hook signals using ``#S`` which
        is just the session name. Raises SendMessageError on timeout.
        """
        wait_channel = f"mngr-submit-{self.session_name}"
        if self._send_enter_and_wait_for_signal(tmux_target, wait_channel):
            logger.debug("Message submitted successfully")
            return

        pane_content = self._capture_pane_content(tmux_target)
        if pane_content is not None:
            logger.error(
                "TUI send enter and wait timeout -- remote pane content:\n{}",
                pane_content,
            )
        else:
            logger.error("TUI send enter and wait timeout -- failed to capture remote pane content")

        self._raise_send_timeout(
            tmux_target,
            f"Timeout waiting for message submission signal (waited {self.enter_submission_timeout_seconds}s)",
        )

    def _send_enter_and_poll_for_input_ready(self, tmux_target: str) -> None:
        """Send Enter and (optionally) poll for the input-cleared indicator.

        Used by agents that lack a UserPromptSubmit-style hook. The
        paste-visibility check in ``_send_message_with_paste_detection`` already
        confirmed the message reached the input field; this path mainly guards
        against the Enter keystroke being swallowed.

        When ``get_tui_input_cleared_indicator()`` returns a string, the path
        is a true submission confirmation: the indicator (typically an input
        placeholder like ``Type your message``) is hidden while the user's
        text occupies the input row and reappears once Enter is consumed and
        the input clears. Interactive TUIs occasionally swallow Enter on
        fresh sessions when it arrives before the pasted text has been
        absorbed -- we retry the keystroke up to
        ``_SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS`` times before raising
        SendMessageError.

        When ``get_tui_input_cleared_indicator()`` returns ``None`` (the
        default), the path degrades to a single fire-and-forget Enter --
        appropriate for agents whose TUI exposes no reliable input-cleared
        signal; those agents accept best-effort submission.
        """
        cleared_indicator = self.get_tui_input_cleared_indicator()
        if cleared_indicator is None:
            self._send_enter_keystroke(tmux_target)
            return

        for attempt in range(_SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS):
            self._send_enter_keystroke(tmux_target)
            if poll_until(
                lambda: self._check_pane_contains(tmux_target, cleared_indicator),
                timeout=_SEND_ENTER_NO_SIGNAL_PER_ATTEMPT_TIMEOUT_SECONDS,
                poll_interval=0.05,
            ):
                logger.trace("Input prompt cleared after Enter attempt {}", attempt + 1)
                return
            logger.debug(
                "Enter attempt {} did not produce TUI input-cleared indicator {!r}; retrying",
                attempt + 1,
                cleared_indicator,
            )

        pane_content = self._capture_pane_content(tmux_target)
        if pane_content is not None:
            logger.error(
                "send-enter-and-poll timeout -- remote pane content:\n{}",
                pane_content,
            )
        total_wait = _SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS * _SEND_ENTER_NO_SIGNAL_PER_ATTEMPT_TIMEOUT_SECONDS
        self._raise_send_timeout(
            tmux_target,
            f"Timeout waiting for TUI input prompt to clear after Enter "
            f"(waited {total_wait:.1f}s across {_SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS} attempts)",
        )

    def _send_enter_keystroke(self, tmux_target: str) -> None:
        """Send a single Enter keystroke via tmux send-keys; raise on failure."""
        send_enter_cmd = f"tmux send-keys -t '{tmux_target}' Enter"
        result = self.host.execute_stateful_command(send_enter_cmd)
        if not result.success:
            raise SendMessageError(
                str(self.name),
                f"tmux send-keys Enter failed: {result.stderr or result.stdout}",
            )

    # FIXME: this function could be improved by having a single command that is running on the host, checking *either* condition (eg, whether we've seen a new enqueue event OR we've seen the wait-for signal)
    #  since either one is sufficient for us to call it success
    def _send_enter_and_wait_for_signal(self, tmux_target: str, wait_channel: str) -> bool:
        """Send Enter and wait for the tmux wait-for signal from the hook.

        Runs ``tmux wait-for`` in the **foreground** so it registers with the
        tmux server synchronously, then sends Enter from a backgrounded
        subshell after a short delay.  This avoids a race where the hook's
        ``tmux wait-for -S`` signal fires before the waiter has registered
        (signals wake exactly one waiter; if none exists the signal is lost).

        The previous implementation backgrounded the waiter, which required a
        double-fork (bash -> timeout -> tmux) before the waiter could register.
        When the agent was actively generating (RUNNING state), the Enter key
        was processed so quickly that the hook signal often fired before the
        waiter finished its double-fork, causing a consistent timeout.

        Returns True if signal received, False if timeout.
        """
        start = time.time()
        timeout_secs = self.enter_submission_timeout_seconds + 1
        last_queue_timestamp = self._get_last_queue_timestamp(timeout_secs)

        remaining_time = timeout_secs - (time.time() - start)
        if remaining_time < 0.0:
            logger.warning(
                "Negative remaining time for wait-for command: {:.2f}s (command execution took too long)",
                remaining_time,
            )
            remaining_time = 5.0

        cmd = (
            f"bash -c '"
            f'( sleep 0.1 && tmux send-keys -t "$1" Enter ) & '
            f'timeout {timeout_secs} tmux wait-for "$0"'
            f"' {shlex.quote(wait_channel)} {shlex.quote(tmux_target)}"
        )
        try:
            result = self.host.execute_stateful_command(cmd, timeout_seconds=remaining_time)
        except TimeoutError:
            # The execute_command timeout can race with the bash `timeout` inside
            # the command. Treat the pyinfra-level timeout as a normal timeout.
            logger.debug("Execute command timed out before bash timeout; treating as signal timeout")
            return False
        elapsed_ms = (time.time() - start) * 1000
        if result.success:
            logger.trace("Received submission signal in {:.0f}ms", elapsed_ms)
            return True

        # if we send a message while another message was being processed, there seems to be a bug in claude where the UserPromptSubmit hook doesn't always fire
        # so in this case, we can also check the session log
        logger.debug("Timeout waiting for submission signal on channel {}, checking session log...", wait_channel)
        remaining_time = timeout_secs - (time.time() - start)
        if remaining_time < 5.0:
            remaining_time = 5.0
        current_queue_timestamp = self._get_last_queue_timestamp(remaining_time)
        if current_queue_timestamp is not None and (
            last_queue_timestamp is None or current_queue_timestamp > last_queue_timestamp
        ):
            # looks like it worked out, it was just annoying and we had to wait.
            logger.trace(
                "Detected new enqueue event in session log with timestamp {}, confirming message submission",
                current_queue_timestamp,
            )
            return True

        return False

    # FIXME: this logic is claude specific, and needs to be refactored so that other agents can properly implement it as well
    def _get_last_queue_timestamp(self, timeout_secs: float) -> str | None:
        env_command_prefix = self.host.build_source_env_prefix(self)
        initial_read_queue_ops_result = self.host.execute_idempotent_command(
            f"""bash -c '{env_command_prefix} cat $MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl | grep "\\"operation\\":\\"enqueue\\"," | tail -n 1 | jq -r .timestamp'""",
            timeout_seconds=timeout_secs + 1,
        )
        last_queue_timestamp = None
        if initial_read_queue_ops_result.success:
            last_queue_timestamp = initial_read_queue_ops_result.stdout
        return last_queue_timestamp
