"""Free-function helpers used by InteractiveTuiAgent and its subclasses.

This module owns the TUI-input pipeline: paste-visibility detection,
TUI-ready polling, and the submit-and-confirm engine. The engine sends
Enter and then polls agent-supplied *durable evidence probes* (transcript
records, marker files) until one proves the agent accepted the message.
Confirmation never relies on ephemeral signals: tmux ``wait-for`` channels
latch signals fired with no waiter (a signal is remembered until the next
waiter consumes it), which historically produced instant false-positive
confirmations that reported success for messages that were never submitted.

The engine runs the whole confirmation window as ONE sequential remote
script -- no background jobs, no EXIT trap -- so Enter is always sent
before any confirmation check can run, and nothing can kill a pending
keystroke.
"""

from __future__ import annotations

import re
import shlex
from enum import auto
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.utils.polling import poll_until

_SEND_MESSAGE_TIMEOUT_SECONDS: Final[float] = 15.0
# This can take a while, especially on Modal -- the process needs to actually
# start and render the TUI before the indicator appears.
_TUI_READY_TIMEOUT_SECONDS: Final[float] = 30.0
# Default confirmation window: how long to poll for durable evidence that the
# agent accepted the message. Needs to be fairly long: a message sent to a busy
# codex/antigravity agent leaves no evidence until the prompt is dequeued at
# the end of the running turn.
DEFAULT_CONFIRMATION_TIMEOUT_SECONDS: Final[float] = 90.0
# Relaxed sends (slash commands) only poll briefly: they never hard-fail, so a
# long window would just delay the caller for commands that leave no evidence.
RELAXED_CONFIRMATION_TIMEOUT_SECONDS: Final[float] = 15.0
# How often the remote confirmation loop re-polls the evidence probes.
_CONFIRMATION_POLL_INTERVAL_SECONDS: Final[float] = 0.5
# Seconds into the confirmation window at which Enter is re-sent when no
# evidence has appeared AND the pane still shows the pasted text (a swallowed
# Enter). Enter is the only thing ever retried -- the text is never re-pasted,
# so mngr's own retries can never duplicate a message.
_ENTER_RETRY_OFFSETS_SECONDS: Final[tuple[int, ...]] = (3, 10, 30)
# Length of the normalized message tail used to gate Enter retries on pane
# content (and, by convention, used by content probes such as Claude's).
NORMALIZED_PROBE_MAX_LENGTH: Final[int] = 60

# Markers printed by the remote confirmation script and parsed by
# _parse_confirmation_output. Kept obscure enough not to collide with probe
# tokens (probes print to command substitutions, never to the script stdout).
_CONFIRMED_MARKER: Final[str] = "MNGR_CONFIRMED"
_TIMEOUT_MARKER: Final[str] = "MNGR_UNCONFIRMED"
_ENTER_FAILED_MARKER: Final[str] = "MNGR_ENTER_FAILED"
_RETRY_MARKER: Final[str] = "MNGR_ENTER_RETRY"
_PROBE_DIAGNOSTIC_MARKER: Final[str] = "MNGR_PROBE"

_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")


class SubmissionConfirmationPolicy(UpperCaseStrEnum):
    """How an unconfirmed submission is surfaced to the caller.

    STRICT raises (normal messages must never silently vanish); RELAXED logs a
    warning and records an agent event (slash commands often leave no
    observable evidence, so they must not hard-fail).
    """

    STRICT = auto()
    RELAXED = auto()


@pure
def is_slash_command_message(message: str) -> bool:
    """Whether a message is a TUI slash command (confirmed under the relaxed policy)."""
    return message.lstrip().startswith("/")


class SubmissionEvidenceProbe(FrozenModel):
    """A durable-evidence source polled to confirm that a message was accepted.

    Both commands are shell snippets run on the agent's host. The probe
    confirms when its poll output is non-empty AND differs from its baseline
    output, so a probe can be either a monotonic token (poll == baseline
    command; e.g. a marker file's mtime) or a baseline-parameterized search
    (baseline captures a byte offset, poll prints a fixed token once the
    evidence is found past ``$base``).
    """

    name: str = Field(description="Short identifier used in logs and diagnostics")
    baseline_command: str = Field(
        description="Shell snippet printing the probe's pre-Enter baseline token (may print nothing)"
    )
    poll_command: str = Field(
        description="Shell snippet printing the probe's current token; may reference the baseline as $base"
    )
    is_rejection: bool = Field(
        default=False,
        description="Whether this probe's evidence proves the agent REJECTED the input (e.g. an unknown command) rather than accepted it",
    )


@pure
def build_changed_token_probe(name: str, token_command: str) -> SubmissionEvidenceProbe:
    """Build a probe that confirms when a monotonic token command's output changes."""
    return SubmissionEvidenceProbe(name=name, baseline_command=token_command, poll_command=token_command)


@pure
def build_file_mtime_token_command(file_path_expression: str) -> str:
    """Portable (GNU + BSD stat) shell snippet printing a file's full-precision mtime token.

    Prints nothing when the file does not exist, so a changed-token probe built
    on this confirms both on the marker appearing and on its mtime advancing.
    ``file_path_expression`` is embedded verbatim (it may reference env vars
    resolved on the host), so the caller is responsible for quoting.
    """
    return f"stat -c %y {file_path_expression} 2>/dev/null || stat -f %Fm {file_path_expression} 2>/dev/null || true"


class SubmissionConfirmationOutcome(FrozenModel):
    """Parsed result of one remote confirmation window."""

    is_confirmed: bool = Field(description="Whether any evidence probe confirmed the submission")
    confirming_probe_name: str | None = Field(description="Name of the probe that confirmed, if any")
    is_rejection: bool = Field(
        default=False,
        description="Whether the confirming probe was a rejection probe (the agent refused the input)",
    )
    enter_retry_offsets: tuple[int, ...] = Field(description="Elapsed seconds at which Enter was re-sent")
    probe_diagnostics: tuple[str, ...] = Field(description="Per-probe baseline/final tokens on timeout")


@pure
def _normalize_for_match(text: str) -> str:
    """Strip non-alphanumeric characters and lowercase for fuzzy matching."""
    return _NON_ALNUM_RE.sub("", text.lower())


@pure
def build_normalized_message_probe(message: str) -> str:
    """Return the normalized tail of a message used for content matching.

    Used both to gate Enter retries on pane content and by content-verifying
    evidence probes (the probe must be compared against text that went through
    the same normalization, e.g. jq-decoded transcript content piped through
    ``tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]'``).
    """
    normalized = _normalize_for_match(message)
    return normalized[-NORMALIZED_PROBE_MAX_LENGTH:]


@pure
def _check_paste_content(pane_content: str, message: str) -> bool:
    """Check whether pasted message content is visible in pane text.

    Returns True if the tmux paste indicator is present OR if a
    normalized tail of the message matches the normalized pane content.
    """
    if "[Pasted text " in pane_content:
        return True

    normalized_pane = _normalize_for_match(pane_content)
    probe = build_normalized_message_probe(message)
    if probe == "":
        return True
    return probe in normalized_pane


def _pane_matches(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget, indicator: str | re.Pattern[str]) -> bool:
    content = agent._capture_pane_content(tmux_target)
    if content is None:
        return False
    if isinstance(indicator, re.Pattern):
        return indicator.search(content) is not None
    return indicator in content


def wait_for_tui_ready(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    indicator: str | re.Pattern[str],
    timeout_seconds: float = _TUI_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until the TUI is ready by polling the pane for ``indicator``.

    ``indicator`` is either a plain ``str`` (matched as an exact substring) or a
    compiled ``re.Pattern`` (matched with ``re.search``) -- the type chooses the
    matching mode. Raises ``SendMessageError`` on timeout. Without this check,
    input sent before the TUI finishes rendering -- or while a resumed transcript
    is still replaying -- may be lost or appear as raw text. Returns immediately
    when the indicator already matches, so it is a cheap no-op once ready.
    """
    label = indicator.pattern if isinstance(indicator, re.Pattern) else indicator
    with log_span("Waiting for TUI to be ready (looking for: {})", label):
        if poll_until(
            lambda: _pane_matches(agent, tmux_target, indicator),
            timeout=timeout_seconds,
        ):
            return
        pane_content = agent._capture_pane_content(tmux_target)
        if pane_content is not None:
            logger.error("TUI ready timeout -- remote pane content:\n{}", pane_content)
        else:
            logger.error("TUI ready timeout -- failed to capture remote pane content")
        raise SendMessageError(
            str(agent.name),
            f"Timeout waiting for TUI to be ready (waited {timeout_seconds:.1f}s)"
            + (f"\nPane content:\n{pane_content}" if pane_content else ""),
        )


def wait_for_paste_visible(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget, message: str) -> None:
    """Wait until pasted content is confirmed visible in the tmux pane.

    Raises ``SendMessageError`` on timeout. Either the tmux paste indicator
    or a fuzzy content match on the last chunk of the message is sufficient
    -- see ``_check_paste_content`` for the details.
    """
    with log_span("Waiting for pasted content to appear"):
        if poll_until(
            lambda: _is_paste_visible(agent, tmux_target, message),
            timeout=_SEND_MESSAGE_TIMEOUT_SECONDS,
        ):
            return
        raise SendMessageError(
            str(agent.name),
            f"Timeout waiting for pasted content to appear (waited {_SEND_MESSAGE_TIMEOUT_SECONDS:.1f}s)",
        )


def _is_paste_visible(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget, message: str) -> bool:
    content = agent._capture_pane_content(tmux_target)
    if content is None:
        return False
    return _check_paste_content(content, message)


def send_enter_keystroke(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget) -> None:
    """Send a single Enter via ``tmux send-keys``; raise SendMessageError on failure."""
    send_enter_cmd = f"tmux send-keys -t {tmux_target.as_shell_arg()} Enter"
    result = agent.host.execute_stateful_command(send_enter_cmd)
    if not result.success:
        raise SendMessageError(
            str(agent.name),
            f"tmux send-keys Enter failed: {result.stderr or result.stdout}",
        )


# ---------------------------------------------------------------------------
# Submit-and-confirm engine
# ---------------------------------------------------------------------------


@pure
def _build_probe_baseline_lines(probes: tuple[SubmissionEvidenceProbe, ...]) -> list[str]:
    """Build the script lines capturing each probe's pre-Enter baseline token."""
    return [f'base_{idx}="$( {probe.baseline_command} )"' for idx, probe in enumerate(probes)]


@pure
def _build_probe_check_lines(probes: tuple[SubmissionEvidenceProbe, ...]) -> list[str]:
    """Build the poll-loop lines that check each probe and exit 0 on confirmation.

    A probe confirms when its poll output is non-empty and differs from its
    baseline. The poll command runs in a command substitution with ``base``
    bound to the probe's baseline token.
    """
    lines: list[str] = []
    for idx, probe in enumerate(probes):
        lines.append(f'cur_{idx}="$( base="$base_{idx}"; {probe.poll_command} )"')
        lines.append(
            f'if [ -n "$cur_{idx}" ] && [ "$cur_{idx}" != "$base_{idx}" ]; then '
            f"printf '%s %s\\n' {_CONFIRMED_MARKER} {shlex.quote(probe.name)}; exit 0; fi"
        )
    return lines


@pure
def _build_enter_retry_lines(
    tmux_target: TmuxWindowTarget,
    normalized_pane_probe: str,
    retry_offsets: tuple[int, ...],
) -> list[str]:
    """Build the poll-loop lines that re-send Enter on schedule.

    One statically generated check per offset, sequenced by ``retry_count``,
    so each slot fires at most once. Retries are gated on the pane still
    showing the pasted text (normalized the same way as the probe) so a retry
    never types into a turn that already consumed the message. When the
    message normalizes to nothing (so the gate cannot recognize it), retries
    fire unconditionally on schedule instead -- a stray Enter on an empty
    input row is a no-op for every TUI we drive. Each slot is consumed whether
    or not Enter was re-sent, bounding the pane captures to one per slot.
    """
    if normalized_pane_probe == "":
        retry_action = (
            f"tmux send-keys -t {tmux_target.as_shell_arg()} Enter 2>/dev/null; "
            f"printf '%s %s\\n' {_RETRY_MARKER} \"$elapsed\""
        )
    else:
        retry_action = (
            f'pane="$( tmux capture-pane -p -t {tmux_target.as_shell_arg()} 2>/dev/null '
            "| tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' )\"; "
            f'case "$pane" in *{shlex.quote(normalized_pane_probe)}*) '
            f"tmux send-keys -t {tmux_target.as_shell_arg()} Enter 2>/dev/null; "
            f"printf '%s %s\\n' {_RETRY_MARKER} \"$elapsed\" ;; *) : ;; esac"
        )
    return [
        f'if [ "$retry_count" -eq {slot_idx} ] && [ "$elapsed" -ge {offset} ]; then '
        f"{retry_action}; retry_count={slot_idx + 1}; fi"
        for slot_idx, offset in enumerate(retry_offsets)
    ]


@pure
def _build_timeout_diagnostic_lines(probes: tuple[SubmissionEvidenceProbe, ...]) -> list[str]:
    """Build the lines printed when the window expires without confirmation."""
    lines = [f"printf '%s\\n' {_TIMEOUT_MARKER}"]
    for idx, probe in enumerate(probes):
        lines.append(
            f"printf '%s %s base=[%s] final=[%s]\\n' {_PROBE_DIAGNOSTIC_MARKER} "
            f'{shlex.quote(probe.name)} "$base_{idx}" "$cur_{idx}"'
        )
    return lines


@pure
def build_confirmation_command(
    tmux_target: TmuxWindowTarget,
    probes: tuple[SubmissionEvidenceProbe, ...],
    normalized_pane_probe: str,
    window_seconds: int,
    poll_interval_seconds: float = _CONFIRMATION_POLL_INTERVAL_SECONDS,
    retry_offsets: tuple[int, ...] = _ENTER_RETRY_OFFSETS_SECONDS,
) -> str:
    """Build the single sequential remote command for one confirmation window.

    The script is deliberately linear -- capture baselines, send Enter, then
    poll -- with no background jobs and no traps, so the ordering invariant
    "confirmation is only ever evaluated after Enter was sent" holds by
    construction. Evidence is checked immediately after Enter and then every
    ``poll_interval_seconds`` until ``window_seconds`` elapse.
    """
    target_arg = tmux_target.as_shell_arg()
    lines: list[str] = []
    # Capture every probe's baseline BEFORE Enter so evidence produced by this
    # submission always reads as a change against it.
    lines.extend(_build_probe_baseline_lines(probes))
    lines.append(f"tmux send-keys -t {target_arg} Enter || {{ printf '%s\\n' {_ENTER_FAILED_MARKER}; exit 3; }}")
    lines.append('start_ts="$( date +%s )"')
    lines.append("retry_count=0")
    lines.append("while :; do")
    loop_lines: list[str] = []
    loop_lines.extend(_build_probe_check_lines(probes))
    loop_lines.append("elapsed=$(( $( date +%s ) - start_ts ))")
    loop_lines.append(f'if [ "$elapsed" -ge {window_seconds} ]; then break; fi')
    loop_lines.extend(_build_enter_retry_lines(tmux_target, normalized_pane_probe, retry_offsets))
    loop_lines.append(f"sleep {poll_interval_seconds}")
    lines.extend(f"  {line}" for line in loop_lines)
    lines.append("done")
    lines.extend(_build_timeout_diagnostic_lines(probes))
    lines.append("exit 1")
    script = "\n".join(lines)
    return f"bash -c {shlex.quote(script)}"


@pure
def _parse_confirmation_output(stdout: str) -> SubmissionConfirmationOutcome:
    """Parse the marker lines printed by the remote confirmation script."""
    confirming_probe_name: str | None = None
    is_confirmed = False
    retry_offsets: list[int] = []
    probe_diagnostics: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(_CONFIRMED_MARKER):
            is_confirmed = True
            probe_name_text = stripped[len(_CONFIRMED_MARKER) :].strip()
            confirming_probe_name = probe_name_text if probe_name_text != "" else None
        elif stripped.startswith(_RETRY_MARKER):
            offset_text = stripped[len(_RETRY_MARKER) :].strip()
            if offset_text.isdigit():
                retry_offsets.append(int(offset_text))
            else:
                probe_diagnostics.append(stripped)
        elif stripped.startswith(_PROBE_DIAGNOSTIC_MARKER):
            probe_diagnostics.append(stripped[len(_PROBE_DIAGNOSTIC_MARKER) :].strip())
        else:
            pass
    return SubmissionConfirmationOutcome(
        is_confirmed=is_confirmed,
        confirming_probe_name=confirming_probe_name,
        enter_retry_offsets=tuple(retry_offsets),
        probe_diagnostics=tuple(probe_diagnostics),
    )


@pure
def _build_unconfirmed_diagnostics(
    outcome: SubmissionConfirmationOutcome,
    window_seconds: int,
    pane_content: str | None,
) -> str:
    """Assemble the human-readable diagnostics for an unconfirmed submission."""
    retry_text = ", ".join(str(offset) for offset in outcome.enter_retry_offsets)
    parts = [
        f"no evidence probe confirmed the submission within {window_seconds}s",
        f"Enter re-sent at offsets: [{retry_text}]" if retry_text != "" else "Enter was never re-sent",
    ]
    if len(outcome.probe_diagnostics) > 0:
        parts.append("probes: " + "; ".join(outcome.probe_diagnostics))
    if pane_content:
        parts.append(f"pane content:\n{pane_content}")
    return "\n".join(parts)


def submit_message_and_confirm(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    message: str,
    probes: tuple[SubmissionEvidenceProbe, ...],
    timeout_seconds: float,
) -> SubmissionConfirmationOutcome:
    """Send Enter to submit the pasted message, then poll evidence probes to confirm.

    Returns the parsed outcome; the caller decides how an unconfirmed outcome
    is surfaced (raise for normal messages, warn for slash commands). Raises
    ``SendMessageError`` only when the Enter keystroke itself could not be
    delivered -- without Enter nothing was submitted under any policy.

    With no probes at all this degrades to a plain best-effort Enter (reported
    as unconfirmed), which is the fallback for agent types that supply no
    durable evidence.
    """
    if len(probes) == 0:
        send_enter_keystroke(agent, tmux_target)
        return SubmissionConfirmationOutcome(
            is_confirmed=False,
            confirming_probe_name=None,
            enter_retry_offsets=(),
            probe_diagnostics=("no evidence probes supplied; Enter sent best-effort",),
        )

    window_seconds = max(1, int(timeout_seconds))
    normalized_pane_probe = build_normalized_message_probe(message)
    command = build_confirmation_command(
        tmux_target=tmux_target,
        probes=probes,
        normalized_pane_probe=normalized_pane_probe,
        window_seconds=window_seconds,
    )
    # Give the remote command a beat past its own internal deadline to return
    # cleanly before the host-level timeout fires.
    try:
        result = agent.host.execute_stateful_command(command, timeout_seconds=window_seconds + 10.0)
    except TimeoutError:
        # The host-level timeout can race with the script's own deadline.
        # Treat it as an ordinary unconfirmed window.
        logger.debug("Confirmation command timed out at the host layer; treating as unconfirmed")
        return SubmissionConfirmationOutcome(
            is_confirmed=False,
            confirming_probe_name=None,
            enter_retry_offsets=(),
            probe_diagnostics=("host-level command timeout before the script deadline",),
        )

    if _ENTER_FAILED_MARKER in result.stdout:
        raise SendMessageError(
            str(agent.name),
            f"tmux send-keys Enter failed: {result.stderr or result.stdout}",
        )

    outcome = _parse_confirmation_output(result.stdout)
    if outcome.is_confirmed:
        rejection_probe_names = {probe.name for probe in probes if probe.is_rejection}
        if outcome.confirming_probe_name in rejection_probe_names:
            outcome = outcome.model_copy(update={"is_rejection": True})
        logger.debug("Message submitted successfully (confirmed by {})", outcome.confirming_probe_name)
        return outcome
    if _TIMEOUT_MARKER not in result.stdout:
        # The script printed neither a confirmation nor its own timeout marker,
        # so it aborted abnormally (e.g. a broken probe command crashed bash)
        # rather than polling out its window. Surface the crash instead of
        # letting it masquerade as an ordinary evidence timeout.
        abort_diagnostic = f"confirmation script aborted abnormally before its deadline; stderr: {result.stderr!r}"
        logger.warning("Confirmation script for agent {} aborted abnormally: {}", agent.name, result.stderr)
        outcome = SubmissionConfirmationOutcome(
            is_confirmed=False,
            confirming_probe_name=None,
            enter_retry_offsets=outcome.enter_retry_offsets,
            probe_diagnostics=(*outcome.probe_diagnostics, abort_diagnostic),
        )
    return outcome


def raise_for_unconfirmed_submission(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    outcome: SubmissionConfirmationOutcome,
    timeout_seconds: float,
) -> None:
    """Raise ``SendMessageError`` with rich diagnostics for an unconfirmed strict send."""
    pane_content = agent._capture_pane_content(tmux_target)
    diagnostics = _build_unconfirmed_diagnostics(outcome, max(1, int(timeout_seconds)), pane_content)
    logger.error("Message submission was not confirmed -- {}", diagnostics)
    raise SendMessageError(str(agent.name), f"Timeout waiting for message submission evidence: {diagnostics}")
