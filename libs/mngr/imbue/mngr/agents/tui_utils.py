"""Free-function helpers used by InteractiveTuiAgent and its subclasses.

This module owns the actual implementation of the TUI-input pipeline:
paste-visibility detection, TUI-ready polling, and the three send-Enter
strategies (signal-hook, poll-cleared-indicator, best-effort). Keeping
them as free functions lets ``InteractiveTuiAgent`` read as a contract
that registers the shape -- each subclass picks a strategy by *calling*
the relevant helper from its ``_send_enter_and_validate`` override.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.utils.polling import poll_until

_SEND_MESSAGE_TIMEOUT_SECONDS: Final[float] = 15.0
# This can take a while, especially on Modal -- the process needs to actually
# start and render the TUI before the indicator appears.
_TUI_READY_TIMEOUT_SECONDS: Final[float] = 30.0
# Default timeout for the signal-based submission path. Needs to be fairly
# long; can be slow when overloading a host while it is starting (esp Modal).
DEFAULT_ENTER_SUBMISSION_WAIT_FOR_TIMEOUT_SECONDS: Final[float] = 90.0
# Bounds for the poll-based Enter path. Each attempt sends Enter then polls
# the pane for the cleared indicator; worst case waits MAX_ATTEMPTS *
# PER_ATTEMPT_TIMEOUT seconds before raising.
_SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS: Final[int] = 3
_SEND_ENTER_NO_SIGNAL_PER_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 2.0

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
# Send-Enter strategies. Subclasses of InteractiveTuiAgent pick one of these
# from their _send_enter_and_validate override.
# ---------------------------------------------------------------------------


def send_enter_best_effort(agent: BaseAgent[Any], tmux_target: TmuxWindowTarget) -> None:
    """Strategy 1: send Enter, do not wait for any confirmation.

    Appropriate when the agent's TUI exposes no submission hook and no
    reliable input-cleared placeholder. The earlier paste-visibility check
    is what gave us confidence the message landed.
    """
    send_enter_keystroke(agent, tmux_target)


def send_enter_and_poll_for_cleared_indicator(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    *,
    cleared_indicator: str,
    max_attempts: int = _SEND_ENTER_NO_SIGNAL_MAX_ATTEMPTS,
    per_attempt_timeout_seconds: float = _SEND_ENTER_NO_SIGNAL_PER_ATTEMPT_TIMEOUT_SECONDS,
) -> None:
    """Strategy 2: send Enter, poll for the cleared indicator, retry on miss.

    ``cleared_indicator`` must be a substring that is hidden while the user's
    text occupies the input row and reappears once Enter is consumed -- the
    typical example is an input-prompt placeholder that the TUI hides while
    the user is typing. When the poll times out we re-send Enter --
    interactive TUIs occasionally swallow Enter on fresh sessions when it
    arrives before the pasted text has been absorbed.

    Raises ``SendMessageError`` if all ``max_attempts`` rounds time out.
    """
    for attempt in range(max_attempts):
        send_enter_keystroke(agent, tmux_target)
        if poll_until(
            lambda: agent._check_pane_contains(tmux_target, cleared_indicator),
            timeout=per_attempt_timeout_seconds,
            poll_interval=0.05,
        ):
            logger.trace("Input prompt cleared after Enter attempt {}", attempt + 1)
            return
        logger.debug(
            "Enter attempt {} did not produce TUI ready indicator {!r}; retrying",
            attempt + 1,
            cleared_indicator,
        )

    pane_content = agent._capture_pane_content(tmux_target)
    if pane_content is not None:
        logger.error("send-enter-and-poll timeout -- remote pane content:\n{}", pane_content)
    total_wait = max_attempts * per_attempt_timeout_seconds
    raise SendMessageError(
        str(agent.name),
        f"Timeout waiting for TUI input prompt to clear after Enter "
        f"(waited {total_wait:.1f}s across {max_attempts} attempts)",
    )


def send_enter_via_tmux_wait_for_hook(
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    *,
    wait_channel: str,
    timeout_seconds: float,
    accept_marker_command: str | None = None,
) -> None:
    """Strategy 3: send Enter and wait for a tmux wait-for channel fired by a TUI hook.

    Used by agents whose TUI exposes a UserPromptSubmit-style hook that fires
    ``tmux wait-for -S $channel`` once the message is submitted. The waiter
    is run in the **foreground** so it registers with the tmux server
    synchronously, then Enter is sent from a backgrounded subshell after a
    short delay. This avoids a race where the hook signal fires before the
    waiter has registered (signals wake exactly one waiter; if none exists
    the signal is lost).

    ``accept_marker_command`` (when supplied) is an agent-provided shell snippet
    that prints a lexicographically-monotonic token -- e.g. an ISO-8601
    timestamp -- for the latest "message accepted" marker its TUI records the
    instant a message is taken into its queue, or empty output if none has been
    recorded yet. Supplying it lets the call also confirm submission from that
    marker, watched *concurrently* with the hook signal. This matters because
    the hook may only fire once the prompt reaches the model -- for a message
    sent to a busy agent that is when it is finally dequeued, potentially much
    later -- whereas the acceptance marker is recorded immediately. We baseline
    the marker before Enter, poll for a newer value alongside the hook wait, and
    succeed on whichever lands first, keeping the call fast (well under any
    front-door HTTP/proxy timeout) for busy agents while the hook still covers
    submissions that record no marker. ``None`` waits on the hook alone (the
    original behavior). Keeping the marker command agent-supplied is what lets
    this module stay agent-neutral.

    Raises ``SendMessageError`` on timeout.
    """
    if _send_enter_and_wait_for_signal(
        agent=agent,
        tmux_target=tmux_target,
        wait_channel=wait_channel,
        timeout_seconds=timeout_seconds,
        accept_marker_command=accept_marker_command,
    ):
        logger.debug("Message submitted successfully")
        return

    pane_content = agent._capture_pane_content(tmux_target)
    if pane_content is not None:
        logger.error(
            "TUI send enter and wait timeout -- remote pane content:\n{}",
            pane_content,
        )
    else:
        logger.error("TUI send enter and wait timeout -- failed to capture remote pane content")

    raise SendMessageError(
        str(agent.name),
        f"Timeout waiting for message submission signal (waited {timeout_seconds}s)",
    )


def _send_enter_and_wait_for_signal(
    *,
    agent: BaseAgent[Any],
    tmux_target: TmuxWindowTarget,
    wait_channel: str,
    timeout_seconds: float,
    accept_marker_command: str | None,
) -> bool:
    """Inner helper for send_enter_via_tmux_wait_for_hook; returns True on success."""
    start = time.time()
    full_timeout = timeout_seconds + 1

    if accept_marker_command is None:
        cmd = _build_signal_only_command(full_timeout, wait_channel, tmux_target)
    else:
        cmd = _build_signal_or_marker_command(full_timeout, wait_channel, tmux_target, accept_marker_command)

    # Give the remote command a beat past its own internal deadline to return
    # cleanly before the pyinfra-level timeout fires.
    remaining_time = full_timeout + 5.0
    try:
        result = agent.host.execute_stateful_command(cmd, timeout_seconds=remaining_time)
    except TimeoutError:
        # The execute_command timeout can race with the bash `timeout` inside
        # the command. Treat the pyinfra-level timeout as a normal timeout.
        logger.debug("Execute command timed out before bash timeout; treating as signal timeout")
        return False
    elapsed_ms = (time.time() - start) * 1000
    if result.success:
        logger.trace("Confirmed message submission in {:.0f}ms", elapsed_ms)
        return True
    return False


def _build_signal_only_command(full_timeout: float, wait_channel: str, tmux_target: TmuxWindowTarget) -> str:
    """The original behavior: send Enter, then block on the hook's wait-for channel.

    Used for TUIs with no acceptance-marker command to watch. The waiter is started (in the
    foreground here) before Enter is sent from a backgrounded subshell, so the
    signal cannot fire before a waiter is registered (signals wake exactly one
    waiter; a signal with none registered is lost).
    """
    return (
        f"bash -c '"
        f'( sleep 0.1 && tmux send-keys -t "$1" Enter ) & '
        f'timeout {full_timeout} tmux wait-for "$0"'
        f"' {shlex.quote(wait_channel)} {tmux_target.as_shell_arg()}"
    )


def _build_signal_or_marker_command(
    full_timeout: float,
    wait_channel: str,
    tmux_target: TmuxWindowTarget,
    accept_marker_command: str,
) -> str:
    """Succeed as soon as EITHER the hook signal fires OR a fresh acceptance marker appears.

    A single remote command so the two conditions are watched concurrently with
    no dangling process: it registers the (full-timeout) hook waiter in the
    background -- which writes a sentinel file on success, preserving the
    register-before-Enter ordering so the signal is never missed (which matters
    for submissions that only ever fire the signal, never recording a marker)
    -- sends Enter, then polls both the sentinel and the acceptance marker until
    either confirms or the deadline passes. Exit 0 = confirmed, non-zero =
    timeout.

    ``accept_marker_command`` is the agent-supplied shell snippet that prints the
    agent's latest acceptance-marker token (empty if none yet). A baseline is
    captured before Enter so only a newer token counts; the comparison is a
    plain string ``>``, so the token must be lexicographically monotonic (an
    ISO-8601 timestamp satisfies this, and an empty baseline sorts before any
    real token). The module stays agent-neutral by treating this command as an
    opaque probe -- all knowledge of the agent's marker schema lives in the
    agent that supplies it.
    """
    script = (
        'sig="$(mktemp)"; '
        # Clean up on every exit path: remove the sentinel file and reap any
        # still-running background job (notably the hook waiter, which otherwise
        # outlives a fast marker-win and would recreate "$sig" -- leaking the
        # temp file -- when the hook finally fires). Runs exactly once on exit.
        'trap \'p="$(jobs -p)"; [ -n "$p" ] && kill $p 2>/dev/null; rm -f "$sig"\' EXIT; '
        f'base="$({accept_marker_command})"; '
        # Register the hook waiter first (full timeout), sentinel on success.
        f'( timeout {full_timeout} tmux wait-for "$1" >/dev/null 2>&1 && echo 1 > "$sig" ) & '
        # Then submit, after a beat so the waiter is registered.
        '( sleep 0.1 && tmux send-keys -t "$2" Enter ) & '
        f'end="$(( $(date +%s) + {int(full_timeout) + 1} ))"; '
        'while [ "$(date +%s)" -lt "$end" ]; do '
        'if [ -s "$sig" ]; then exit 0; fi; '
        f'cur="$({accept_marker_command})"; '
        'if [[ -n "$cur" && "$cur" > "$base" ]]; then exit 0; fi; '
        "sleep 0.25; "
        "done; "
        "exit 1"
    )
    return f"bash -c {shlex.quote(script)} _ {shlex.quote(wait_channel)} {tmux_target.as_shell_arg()}"
