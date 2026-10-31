"""Free-function helpers used by InteractiveTuiAgent and its subclasses.

This module owns the TUI-input pipeline: TUI-ready polling and the
submit-and-confirm engine. The engine waits for the typed message to show in
the pane, sends Enter, and then polls agent-supplied *evidence probes*
(transcript records, marker files, the agent's screen) until one proves the
agent accepted the message.
Confirmation never relies on tmux ``wait-for`` channels: they latch signals
fired with no waiter (a signal is remembered until the next
waiter consumes it), which historically produced instant false-positive
confirmations that reported success for messages that were never submitted.

The engine runs the paste wait and the whole confirmation window as ONE
sequential remote script -- no background jobs, no EXIT trap -- so Enter is
always sent before any confirmation check can run, and nothing can kill a
pending keystroke. One script is also one process spawn from mngr, which
matters under gVisor, where each spawn costs tens of milliseconds.
"""

from __future__ import annotations

import re
import shlex
from enum import auto
from typing import Any
from typing import Callable
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import describe_failed_send_command
from imbue.mngr.errors import PastedMessageNotShownError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.utils.polling import poll_until

# How long the typed message may take to show in the pane before the send fails without Enter.
_PASTE_VISIBLE_TIMEOUT_SECONDS: Final[int] = 15
# How often the remote script re-reads the pane while waiting for the typed message to show.
# Each check spawns tmux and two tr, so polling faster would trade spawns for little latency.
_PASTE_VISIBLE_POLL_INTERVAL_SECONDS: Final[float] = 0.1
# This can take a while, especially on Modal -- the process needs to actually
# start and render the TUI before the indicator appears.
TUI_READY_TIMEOUT_SECONDS: Final[float] = 30.0
# Default confirmation window: how long to poll for evidence that the
# agent accepted the message. Needs to be fairly long: a message sent to a busy
# codex/antigravity agent leaves no evidence until the prompt is dequeued at
# the end of the running turn.
DEFAULT_CONFIRMATION_TIMEOUT_SECONDS: Final[float] = 90.0
# Relaxed sends (slash commands) only poll briefly: they never hard-fail, so a
# long window would just delay the caller for commands that leave no evidence.
RELAXED_CONFIRMATION_TIMEOUT_SECONDS: Final[float] = 15.0
# How often the remote confirmation loop re-polls the evidence probes: briskly for the first
# seconds after Enter, when an idle or queueing agent records the message, then more slowly, so a
# long window spent waiting on a busy agent does not keep spawning probe processes.
_FAST_CONFIRMATION_POLL_INTERVAL_SECONDS: Final[float] = 0.1
_FAST_CONFIRMATION_POLL_WINDOW_SECONDS: Final[int] = 2
_CONFIRMATION_POLL_INTERVAL_SECONDS: Final[float] = 0.5
# Seconds into the confirmation window at which Enter is re-sent when no
# evidence has appeared and the agent type's retry gate, if it has one, passes
# (a swallowed Enter). Enter is the only thing ever retried -- the text is never
# re-pasted, so mngr's own retries can never duplicate a message.
_ENTER_RETRY_OFFSETS_SECONDS: Final[tuple[int, ...]] = (3, 10, 30)
# Length of the normalized message tail matched against the pane (paste wait and
# Enter retries) and, by convention, against transcripts by content probes such as Claude's.
NORMALIZED_PROBE_MAX_LENGTH: Final[int] = 60
# How long to keep observing the pane after a send has been confirmed delivered before
# concluding that no blocking dialog appeared -- a selector (e.g. Claude's /model
# confirmation) can render a beat after the input is accepted. Also used as the per-accept
# poll window while clearing chained dialogs. Serves as the default for the Claude agent's
# per-agent ``post_submit_dialog_observe_seconds`` config field (which overrides it at runtime).
POST_SUBMIT_DIALOG_OBSERVE_SECONDS: Final[float] = 2.0

# Markers printed by the remote confirmation script and parsed by
# _parse_confirmation_output. Kept obscure enough not to collide with probe
# tokens (probes print to command substitutions, never to the script stdout).
_PASTE_NOT_VISIBLE_MARKER: Final[str] = "MNGR_PASTE_NOT_VISIBLE"
_CONFIRMED_MARKER: Final[str] = "MNGR_CONFIRMED"
_TIMEOUT_MARKER: Final[str] = "MNGR_UNCONFIRMED"
_ENTER_FAILED_MARKER: Final[str] = "MNGR_ENTER_FAILED"
_RETRY_MARKER: Final[str] = "MNGR_ENTER_RETRY"
_PROBE_DIAGNOSTIC_MARKER: Final[str] = "MNGR_PROBE"

_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")
_NON_ASCII_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]")

# Shell function the remote script defines to ask whether the pane still shows the unsubmitted
# message: the TUI's paste placeholder verbatim, or the message probe in the pane's text after
# the same normalization as ``_build_pane_message_probe`` (byte-wise under the C locale, so it
# does not depend on the host's locale).
_PANE_SHOWS_MESSAGE_FUNCTION: Final[str] = "pane_shows_message"


class SubmissionConfirmationPolicy(UpperCaseStrEnum):
    """How an unconfirmed submission is surfaced to the caller.

    STRICT raises (normal messages must never silently vanish); RELAXED logs a
    warning and records an agent event (slash commands often leave no
    observable evidence, so they must not hard-fail).
    """

    STRICT = auto()
    RELAXED = auto()


class UnconfirmedSubmissionReading(UpperCaseStrEnum):
    """What a TUI's own screen says about a send that is not confirmed: no probe confirmed it, or Enter was never sent.

    TAKEN: the TUI holds the message and will record it when it can. NOT_TAKEN: the message never
    left the input box, and was cleared from it. UNKNOWN: the screen says neither.
    """

    TAKEN = auto()
    NOT_TAKEN = auto()
    UNKNOWN = auto()


@pure
def is_slash_command_message(message: str) -> bool:
    """Whether a message is a TUI slash command (confirmed under the relaxed policy)."""
    return message.lstrip().startswith("/")


# The confirmation script's variable holding the tmux target of the pane the message was typed into,
# for a probe that reads the agent's screen: the pane's ID, or an exact-match ``=session:window``,
# with the rendered target's shell quoting already removed, so a probe passes it to ``-t`` as it is.
PANE_TARGET_SHELL_VARIABLE: Final[str] = "mngr_pane_target"


class SubmissionEvidenceProbe(FrozenModel):
    """An evidence source (a transcript, a marker file, the agent's screen) polled to confirm a message was accepted.

    Both commands are shell snippets run on the agent's host. The probe
    confirms when its poll output is non-empty AND differs from its baseline
    output, so a probe can be either a monotonic token (poll == baseline
    command; e.g. a marker file's mtime) or a baseline-parameterized search
    (baseline captures a byte offset, poll prints a fixed token once the
    evidence is found past ``$base``).
    """

    name: str = Field(description="Short identifier used in logs and diagnostics")
    baseline_command: str = Field(
        description=(
            "Shell snippet printing the probe's pre-Enter baseline token (may print nothing); may reference the "
            f"agent's pane as ${PANE_TARGET_SHELL_VARIABLE}"
        )
    )
    poll_command: str = Field(
        description=(
            "Shell snippet printing the probe's current token; may reference the baseline as $base, and the "
            f"agent's pane as ${PANE_TARGET_SHELL_VARIABLE}"
        )
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


class EnterRetryGate(FrozenModel):
    """A pane test that decides whether a swallowed Enter may be re-sent during a confirmation window."""

    condition_command: NonEmptyStr = Field(
        description=(
            "Shell snippet run in a subshell with $raw_pane bound to a plain capture of the agent's pane; "
            "it exits 0 while the pane shows the message still waiting to be submitted, and its stdout is discarded"
        )
    )


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

    Used by content-verifying evidence probes (the probe must be compared
    against text that went through the same normalization, e.g. jq-decoded
    transcript content piped through
    ``tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]'``).
    """
    normalized = _normalize_for_match(message)
    return normalized[-NORMALIZED_PROBE_MAX_LENGTH:]


@pure
def _build_pane_message_probe(message: str) -> str:
    """Return the message tail the remote script looks for in the pane.

    Keeps only ASCII letters and digits before lowercasing, exactly what the script's
    ``LC_ALL=C tr`` pipeline leaves of the pane, so the message as typed always matches itself.
    """
    return _NON_ASCII_ALNUM_RE.sub("", message)[-NORMALIZED_PROBE_MAX_LENGTH:].lower()


@pure
def is_pane_content_matching_indicator(
    pane_content: str, indicator: str | re.Pattern[str] | Callable[[str], bool]
) -> bool:
    """Whether pane content satisfies a TUI-ready indicator (see ``wait_for_tui_ready`` for the matching modes)."""
    if isinstance(indicator, re.Pattern):
        return indicator.search(pane_content) is not None
    if isinstance(indicator, str):
        return indicator in pane_content
    return indicator(pane_content)


def _pane_matches(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget | str,
    indicator: str | re.Pattern[str] | Callable[[str], bool],
) -> bool:
    content = agent._capture_pane_content(tmux_target)
    return content is not None and is_pane_content_matching_indicator(content, indicator)


def wait_for_tui_ready(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget | str,
    indicator: str | re.Pattern[str] | Callable[[str], bool],
    timeout_seconds: float = TUI_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until the TUI is ready by polling the pane for ``indicator``.

    ``indicator`` is a plain ``str`` (matched as an exact substring), a compiled
    ``re.Pattern`` (matched with ``re.search``), or a predicate taking the pane
    content -- the type chooses the matching mode. A predicate is what a harness
    needs when readiness depends on WHERE something appears rather than whether
    it appears at all. Raises ``SendMessageError`` on timeout. Without this check,
    input sent before the TUI finishes rendering -- or while a resumed transcript
    is still replaying -- may be lost or appear as raw text. Returns immediately
    when the indicator already matches, so it is a cheap no-op once ready.
    """
    if isinstance(indicator, re.Pattern):
        label = indicator.pattern
    elif isinstance(indicator, str):
        label = indicator
    else:
        # A predicate is described rather than named: `Callable` declares no `__name__`, and
        # the alternatives (a Protocol carrying one, or getattr) buy a function name that says
        # no more here than this does.
        label = "a readiness predicate over the pane"
    # Resolved once, so each poll costs one capture rather than a lookup and a capture.
    resolved_target = agent._send_target_arg(tmux_target)
    with log_span("Waiting for TUI to be ready (looking for: {})", label):
        if poll_until(
            lambda: _pane_matches(agent, resolved_target, indicator),
            timeout=timeout_seconds,
        ):
            return
        pane_content = agent._capture_pane_content(resolved_target)
        if pane_content is not None:
            logger.error("TUI ready timeout -- remote pane content:\n{}", pane_content)
        else:
            logger.error("TUI ready timeout -- failed to capture remote pane content")
        # The pane is logged just above but deliberately kept OUT of the raised message: it is
        # unbounded and can carry the user's own code or a diff, and this message is surfaced.
        raise SendMessageError(
            str(agent.name),
            f"Timeout waiting for TUI to be ready (waited {timeout_seconds:.1f}s)",
            SendFailureKind.NOT_READY,
        )


def send_key_keystroke(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget | str, key: str) -> None:
    """Send a single named key (e.g. ``Enter``, ``BSpace``) via ``tmux send-keys``.

    ``key`` is a tmux key name, not literal text, so it is spliced into the
    command unquoted (as tmux expects). Raises SendMessageError on failure.
    """
    # The agent's pane, not the window's active one: a split would otherwise send this keystroke
    # into whatever shell the user just opened.
    agent._run_send_command(f"tmux send-keys -t {agent._send_target_arg(tmux_target)} {key}", f"tmux send-keys {key}")


def send_enter_keystroke(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget | str) -> None:
    """Send a single Enter via ``tmux send-keys``; raise SendMessageError on failure."""
    send_key_keystroke(agent, tmux_target, "Enter")


# Submit-and-confirm engine


@pure
def _build_unsubmitted_message_checks(
    pane_message_probe: str, paste_placeholder_prefixes: tuple[str, ...], on_match: str
) -> list[str]:
    """Build the shell checks that run ``on_match`` when ``$raw_pane`` shows a paste placeholder or the message tail."""
    placeholder_patterns = "|".join(f"*{shlex.quote(prefix)}*" for prefix in paste_placeholder_prefixes)
    placeholder_checks = (
        [f'case "$raw_pane" in {placeholder_patterns}) {on_match} ;; esac'] if placeholder_patterns else []
    )
    return [
        *placeholder_checks,
        "pane=\"$( printf '%s' \"$raw_pane\" | LC_ALL=C tr -cd 'A-Za-z0-9' | LC_ALL=C tr 'A-Z' 'a-z' )\"",
        f'case "$pane" in *{shlex.quote(pane_message_probe)}*) {on_match} ;; esac',
    ]


@pure
def build_unsubmitted_message_retry_gate(
    message: str, paste_placeholder_prefixes: tuple[str, ...]
) -> EnterRetryGate | None:
    """Build the gate that allows a retry while the pane shows the message's tail or a paste placeholder.

    Returns None when the message has no ASCII letters or digits to look for in the pane.
    """
    pane_message_probe = _build_pane_message_probe(message)
    if pane_message_probe == "":
        return None
    checks = _build_unsubmitted_message_checks(pane_message_probe, paste_placeholder_prefixes, "exit 0")
    return EnterRetryGate(condition_command=NonEmptyStr("\n".join([*checks, "exit 1"])))


@pure
def _build_pane_shows_message_function_lines(
    target_arg: str, pane_message_probe: str, paste_placeholder_prefixes: tuple[str, ...]
) -> list[str]:
    """Define the script's pane check (see ``_PANE_SHOWS_MESSAGE_FUNCTION``)."""
    checks = _build_unsubmitted_message_checks(pane_message_probe, paste_placeholder_prefixes, "return 0")
    return [
        f"{_PANE_SHOWS_MESSAGE_FUNCTION}() {{",
        f'  raw_pane="$( tmux capture-pane -p -t {target_arg} 2>/dev/null )"',
        *(f"  {line}" for line in checks),
        "  return 1",
        "}",
    ]


@pure
def _build_paste_wait_lines(paste_wait_seconds: int) -> list[str]:
    """Build the lines that wait for the typed message to show, and give up without Enter if it never does."""
    return [
        f"paste_deadline=$(( SECONDS + {paste_wait_seconds} ))",
        f"until {_PANE_SHOWS_MESSAGE_FUNCTION}; do",
        f'  if [ "$SECONDS" -ge "$paste_deadline" ]; then printf \'%s\\n\' {_PASTE_NOT_VISIBLE_MARKER}; exit 4; fi',
        f"  sleep {_PASTE_VISIBLE_POLL_INTERVAL_SECONDS}",
        "done",
    ]


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
def _build_retry_gate_check(enter_retry_gate: EnterRetryGate) -> str:
    """Build the shell test that runs the gate's condition in a subshell with its stdout discarded."""
    # On its own lines so a trailing ``#`` comment in the condition cannot swallow the closing parenthesis.
    return f"(\n{enter_retry_gate.condition_command}\n) >/dev/null"


@pure
def _build_enter_retry_lines(
    target_arg: str,
    enter_retry_gate: EnterRetryGate | None,
    retry_offsets: tuple[int, ...],
) -> list[str]:
    """Build the poll-loop lines that re-send Enter on schedule.

    One statically generated check per offset, sequenced by ``retry_count``,
    so each slot fires at most once. Each retry captures the pane and re-sends
    Enter only when the gate's condition exits 0; with no gate, Enter is
    re-sent at every offset. Each slot is consumed whether or not Enter was
    re-sent, bounding the pane captures to one per slot.
    """
    send_retry = f"tmux send-keys -t {target_arg} Enter 2>/dev/null; printf '%s %s\\n' {_RETRY_MARKER} \"$elapsed\""
    retry_action = (
        send_retry
        if enter_retry_gate is None
        else (
            f'raw_pane="$( tmux capture-pane -p -t {target_arg} 2>/dev/null )"; '
            f"if {_build_retry_gate_check(enter_retry_gate)}; then {send_retry}; fi"
        )
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
    tmux_target: TmuxWindowTarget | str,
    probes: tuple[SubmissionEvidenceProbe, ...],
    pane_message_probe: str,
    paste_placeholder_prefixes: tuple[str, ...],
    enter_retry_gate: EnterRetryGate | None,
    window_seconds: int,
    paste_wait_seconds: int = _PASTE_VISIBLE_TIMEOUT_SECONDS,
    retry_offsets: tuple[int, ...] = _ENTER_RETRY_OFFSETS_SECONDS,
) -> str:
    """Build the single sequential remote command that submits a typed message and confirms it.

    The script is deliberately linear -- wait for the typed message to show,
    capture baselines, send Enter, then poll -- with no background jobs and no
    traps, so the ordering invariant "confirmation is only ever evaluated after
    Enter was sent" holds by construction. A message that never shows exits
    without Enter. Evidence is checked immediately after Enter and then on the
    poll schedule until ``window_seconds`` elapse; with no probes the script
    stops once Enter is sent. Elapsed time comes from bash's ``SECONDS`` rather
    than ``date``, which would cost a process per poll.
    """
    # Accepts a rendered target so the caller can hand in the agent's pane ID. Everything this
    # script does -- the paste wait, the Enter, the retries -- must address the same pane the
    # typing went to, or a split turns each pane read into a reading of another shell.
    target_arg = tmux_target if isinstance(tmux_target, str) else tmux_target.as_shell_arg()
    is_pane_message_probe_empty = pane_message_probe == ""
    lines: list[str] = [f"{PANE_TARGET_SHELL_VARIABLE}={target_arg}"]
    if not is_pane_message_probe_empty:
        lines.extend(
            _build_pane_shows_message_function_lines(target_arg, pane_message_probe, paste_placeholder_prefixes)
        )
        lines.extend(_build_paste_wait_lines(paste_wait_seconds))
    # Capture every probe's baseline BEFORE Enter so evidence produced by this
    # submission always reads as a change against it.
    lines.extend(_build_probe_baseline_lines(probes))
    lines.append(f"tmux send-keys -t {target_arg} Enter || {{ printf '%s\\n' {_ENTER_FAILED_MARKER}; exit 3; }}")
    if len(probes) == 0:
        lines.append("exit 0")
    else:
        lines.append("start_ts=$SECONDS")
        lines.append("retry_count=0")
        lines.append("while :; do")
        loop_lines: list[str] = []
        loop_lines.extend(_build_probe_check_lines(probes))
        loop_lines.append("elapsed=$(( SECONDS - start_ts ))")
        loop_lines.append(f'if [ "$elapsed" -ge {window_seconds} ]; then break; fi')
        loop_lines.extend(_build_enter_retry_lines(target_arg, enter_retry_gate, retry_offsets))
        loop_lines.append(
            f'if [ "$elapsed" -lt {_FAST_CONFIRMATION_POLL_WINDOW_SECONDS} ]; '
            f"then sleep {_FAST_CONFIRMATION_POLL_INTERVAL_SECONDS}; else sleep {_CONFIRMATION_POLL_INTERVAL_SECONDS}; fi"
        )
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
    tmux_target: TmuxWindowTarget | str,
    message: str,
    probes: tuple[SubmissionEvidenceProbe, ...],
    paste_placeholder_prefixes: tuple[str, ...],
    enter_retry_gate: EnterRetryGate | None,
    timeout_seconds: float,
) -> SubmissionConfirmationOutcome:
    """Wait for the typed message to show, send Enter to submit it, then poll evidence probes to confirm.

    Returns the parsed outcome; the caller decides how an unconfirmed outcome
    is surfaced (raise for normal messages, warn for slash commands). Raises
    ``PastedMessageNotShownError`` when the message never showed in the pane
    (Enter is then never sent), and ``SendMessageError`` when the Enter keystroke
    itself could not be delivered -- without Enter nothing was submitted under
    any policy. With no probes, a
    script that failed or timed out raises too, since nothing else would show
    whether Enter was sent.

    With no probes at all this degrades to a plain best-effort Enter (reported
    as unconfirmed), which is the fallback for agent types that supply no
    durable evidence. ``paste_placeholder_prefixes`` are what the input box shows
    in place of a pasted message; ``enter_retry_gate`` decides whether a
    swallowed Enter is re-sent, and None re-sends it on schedule without
    checking the pane.
    """
    window_seconds = max(1, int(timeout_seconds))
    command = build_confirmation_command(
        tmux_target=agent._send_target_arg(tmux_target),
        probes=probes,
        pane_message_probe=_build_pane_message_probe(message),
        paste_placeholder_prefixes=paste_placeholder_prefixes,
        enter_retry_gate=enter_retry_gate,
        window_seconds=window_seconds,
    )
    # Give the remote command a beat past its own internal deadlines to return
    # cleanly before the host-level timeout fires. Without probes the script
    # stops at Enter, so the Enter's own send bound replaces the window.
    host_timeout_seconds = _PASTE_VISIBLE_TIMEOUT_SECONDS + (
        window_seconds + 10.0 if len(probes) > 0 else agent.send_command_timeout_seconds
    )
    try:
        with log_span("Waiting for the typed message to show, then submitting and confirming it"):
            result = agent.host.execute_stateful_command(command, timeout_seconds=host_timeout_seconds)
    except TimeoutError:
        if len(probes) == 0:
            raise SendMessageError(
                str(agent.name), f"submitting the message timed out after {host_timeout_seconds:.0f}s"
            ) from None
        # The host-level timeout can race with the script's own deadline.
        # Treat it as an ordinary unconfirmed window.
        logger.debug("Confirmation command timed out at the host layer; treating as unconfirmed")
        return SubmissionConfirmationOutcome(
            is_confirmed=False,
            confirming_probe_name=None,
            enter_retry_offsets=(),
            probe_diagnostics=("host-level command timeout before the script deadline",),
        )

    if _PASTE_NOT_VISIBLE_MARKER in result.stdout:
        raise PastedMessageNotShownError(
            str(agent.name),
            f"Timeout waiting for pasted content to appear (waited {_PASTE_VISIBLE_TIMEOUT_SECONDS:.1f}s)",
        )
    if _ENTER_FAILED_MARKER in result.stdout:
        raise SendMessageError(
            str(agent.name),
            f"tmux send-keys Enter failed: {result.stderr or result.stdout}",
        )
    if len(probes) == 0:
        if not result.success:
            raise SendMessageError(
                str(agent.name),
                f"submitting the message failed: {describe_failed_send_command(result, host_timeout_seconds)}",
            )
        return SubmissionConfirmationOutcome(
            is_confirmed=False,
            confirming_probe_name=None,
            enter_retry_offsets=(),
            probe_diagnostics=("no evidence probes supplied; Enter sent best-effort",),
        )

    outcome = _parse_confirmation_output(result.stdout)
    if outcome.is_confirmed:
        rejection_probe_names = {probe.name for probe in probes if probe.is_rejection}
        if outcome.confirming_probe_name in rejection_probe_names:
            outcome = outcome.model_copy_update(to_update(outcome.field_ref().is_rejection, True))
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
    tmux_target: TmuxWindowTarget | str,
    outcome: SubmissionConfirmationOutcome,
    timeout_seconds: float,
) -> None:
    """Raise ``SendMessageError`` with rich diagnostics for an unconfirmed strict send."""
    pane_content = agent._capture_pane_content(tmux_target)
    diagnostics = _build_unconfirmed_diagnostics(outcome, max(1, int(timeout_seconds)), pane_content)
    logger.error("Message submission was not confirmed -- {}", diagnostics)
    raise SendMessageError(
        str(agent.name),
        f"Timeout waiting for message submission evidence: {diagnostics}",
        SendFailureKind.SUBMISSION_UNCONFIRMED,
    )
