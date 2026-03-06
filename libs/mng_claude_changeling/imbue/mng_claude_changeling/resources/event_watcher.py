#!/usr/bin/env python3
"""Event watcher for changeling agents.

Streams events from ``mng events --follow --filter`` and delivers them
to the primary agent via ``mng message`` with debouncing and rate limiting.

The watcher delegates all event discovery, deduplication, and filtering
to the ``mng events`` command (run as a subprocess). This script handles:

- At-least-once delivery with minimal duplicates on restart
- Token-bucket rate limiting (burst + sustained rate)
- Delivery envelope formatting (time since last message, rate warnings)
- Subprocess lifecycle (restart on exit)

Usage: python3 event_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
  MNG_AGENT_WORK_DIR   - agent working directory (contains changelings.toml)
  MNG_AGENT_NAME       - name of the primary agent to send messages to
  MNG_HOST_DIR         - host data directory (for log output)
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger

try:
    from imbue.mng_claude_changeling.resources.watcher_common import DEFAULT_CEL_FILTER
    from imbue.mng_claude_changeling.resources.watcher_common import MngNotInstalledError
    from imbue.mng_claude_changeling.resources.watcher_common import get_mng_command
    from imbue.mng_claude_changeling.resources.watcher_common import load_watchers_section
    from imbue.mng_claude_changeling.resources.watcher_common import require_env
    from imbue.mng_claude_changeling.resources.watcher_common import setup_watcher_logging
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from watcher_common import DEFAULT_CEL_FILTER  # type: ignore[no-redef]
    from watcher_common import MngNotInstalledError  # type: ignore[no-redef]
    from watcher_common import get_mng_command  # type: ignore[no-redef]
    from watcher_common import load_watchers_section  # type: ignore[no-redef]
    from watcher_common import require_env  # type: ignore[no-redef]
    from watcher_common import setup_watcher_logging  # type: ignore[no-redef]


# -- Constants --
# NOTE: These defaults must be kept in sync with the Field defaults in
# data_types.py WatcherSettings. They are duplicated here because this
# script runs standalone on the host and cannot import from data_types.py.


_DEFAULT_BURST_SIZE: Final[int] = 5
_DEFAULT_MAX_MESSAGES_PER_MINUTE: Final[int] = 10
_DEFAULT_HIGH_RATE_WARNING_THRESHOLD: Final[int] = 8
_DEFAULT_MAX_DELIVERY_RETRIES: Final[int] = 3

_DELIVERY_STATE_FILENAME: Final[str] = ".event_delivery_state.json"

_SUBPROCESS_RESTART_DELAY_SECONDS: Final[float] = 5.0

_MESSAGE_SEND_TIMEOUT_SECONDS: Final[float] = 120.0

# How often the delivery loop polls for buffered events
_DELIVERY_POLL_INTERVAL_SECONDS: Final[float] = 0.5

# Exponential backoff for delivery retries
_BACKOFF_BASE_SECONDS: Final[float] = 2.0
_BACKOFF_MAX_SECONDS: Final[float] = 60.0


# -- Settings --


@dataclasses.dataclass(frozen=True)
class _EventWatcherSettings:
    """Parsed event watcher settings from settings.toml."""

    cel_filter: str = DEFAULT_CEL_FILTER
    burst_size: int = _DEFAULT_BURST_SIZE
    max_messages_per_minute: int = _DEFAULT_MAX_MESSAGES_PER_MINUTE
    high_rate_warning_threshold: int = _DEFAULT_HIGH_RATE_WARNING_THRESHOLD
    max_delivery_retries: int = _DEFAULT_MAX_DELIVERY_RETRIES


def _load_watcher_settings(agent_work_dir: Path) -> _EventWatcherSettings:
    """Load event watcher settings from settings.toml, falling back to defaults."""
    watchers = load_watchers_section(agent_work_dir)
    if not watchers:
        return _EventWatcherSettings()
    return _EventWatcherSettings(
        cel_filter=watchers.get("event_cel_filter", DEFAULT_CEL_FILTER),
        burst_size=watchers.get("event_burst_size", _DEFAULT_BURST_SIZE),
        max_messages_per_minute=watchers.get("max_event_messages_per_minute", _DEFAULT_MAX_MESSAGES_PER_MINUTE),
        high_rate_warning_threshold=watchers.get(
            "high_rate_warning_threshold_per_minute", _DEFAULT_HIGH_RATE_WARNING_THRESHOLD
        ),
        max_delivery_retries=watchers.get("max_delivery_retries", _DEFAULT_MAX_DELIVERY_RETRIES),
    )


# -- Delivery state --


@dataclasses.dataclass
class _DeliveryState:
    """Tracks which events have been delivered for at-least-once semantics."""

    last_event_id: str = ""
    last_timestamp: str = ""
    last_delivery_monotonic: float = 0.0


def _load_delivery_state(state_file: Path) -> _DeliveryState:
    """Load delivery state from file, returning defaults if not found or corrupt."""
    try:
        if not state_file.is_file():
            return _DeliveryState()
        raw = json.loads(state_file.read_text())
        return _DeliveryState(
            last_event_id=raw.get("last_event_id", ""),
            last_timestamp=raw.get("last_timestamp", ""),
        )
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Failed to load delivery state from {}: {}", state_file, exc)
        return _DeliveryState()


def _save_delivery_state(state_file: Path, state: _DeliveryState) -> None:
    """Persist delivery state to file atomically (write tmp + rename)."""
    data = {"last_event_id": state.last_event_id, "last_timestamp": state.last_timestamp}
    tmp_file = state_file.with_suffix(".tmp")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(json.dumps(data))
        tmp_file.rename(state_file)
    except OSError as exc:
        logger.error("Failed to save delivery state to {}: {}", state_file, exc)


# -- Rate limiting --


class _TokenBucket:
    """Token bucket rate limiter.

    Starts with burst_size tokens. Tokens refill at rate_per_second.
    Accepts an optional time_source for deterministic testing.
    """

    def __init__(
        self,
        burst_size: int,
        rate_per_second: float,
        # Callable returning monotonic seconds, injectable for testing
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst_size = burst_size
        self._rate_per_second = rate_per_second
        self._tokens = float(burst_size)
        self._time_source = time_source
        self._last_refill_time = time_source()

    def consume(self) -> bool:
        """Try to consume one token. Returns True if a token was available."""
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def time_until_token(self) -> float:
        """Seconds until next token is available. Returns 0.0 if one is available now."""
        self._refill()
        if self._tokens >= 1.0:
            return 0.0
        deficit = 1.0 - self._tokens
        if self._rate_per_second <= 0:
            return float("inf")
        return deficit / self._rate_per_second

    def _refill(self) -> None:
        now = self._time_source()
        elapsed = now - self._last_refill_time
        self._last_refill_time = now
        self._tokens = min(float(self._burst_size), self._tokens + elapsed * self._rate_per_second)


class _SendRateTracker:
    """Tracks message send rate over a sliding 60-second window."""

    def __init__(self) -> None:
        self._send_times: list[float] = []

    def record_send(self) -> None:
        self._send_times.append(time.monotonic())
        self._prune()

    def messages_per_minute(self) -> float:
        """Return the number of messages sent in the last 60 seconds."""
        self._prune()
        return float(len(self._send_times))

    def _prune(self) -> None:
        cutoff = time.monotonic() - 60.0
        self._send_times = [t for t in self._send_times if t > cutoff]


# -- Catch-up filtering --


def _should_skip_for_catchup(line_json: dict[str, Any], delivery_state: _DeliveryState) -> bool:
    """Return True if this event was already delivered in a prior run and should be skipped.

    Uses < for timestamp comparison (not <=) to ensure at-least-once semantics:
    events sharing the same timestamp as the last delivered event may be
    re-delivered on restart, which is acceptable for at-least-once delivery.
    """
    if not delivery_state.last_event_id and not delivery_state.last_timestamp:
        return False

    event_id = line_json.get("event_id", "")
    if event_id and event_id == delivery_state.last_event_id:
        return True

    timestamp = line_json.get("timestamp", "")
    if timestamp and delivery_state.last_timestamp and timestamp < delivery_state.last_timestamp:
        return True

    return False


# -- Message formatting --


def _format_time_since_last(seconds: float) -> str:
    """Format a duration as a human-readable string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        return f"{seconds / 3600:.1f}h"


def _format_delivery_message(
    event_lines: list[str],
    time_since_last_message_seconds: float | None,
    rate_warning: str | None,
) -> str:
    """Format a delivery envelope with event lines and metadata."""
    parts: list[str] = []

    # Header
    count = len(event_lines)
    parts.append(f"[Event watcher] {count} new event(s)")

    if time_since_last_message_seconds is not None:
        parts.append(f"  Time since last message: {_format_time_since_last(time_since_last_message_seconds)}")

    if rate_warning:
        parts.append(f"  WARNING: {rate_warning}")

    parts.append("")

    for line in event_lines:
        parts.append(line)

    return "\n".join(parts)


# -- Message sending --


def _send_message(agent_name: str, message: str) -> bool:
    """Send a message to the agent via mng message. Returns True on success."""
    try:
        result = subprocess.run(
            [*get_mng_command(), "message", agent_name, "--provider", "local", "-m", message],
            capture_output=True,
            text=True,
            timeout=_MESSAGE_SEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out sending message to {}", agent_name)
        return False
    except (OSError, MngNotInstalledError) as exc:
        logger.error("Failed to invoke mng message subprocess: {}", exc)
        return False

    if result.returncode != 0:
        logger.error("mng message returned non-zero for {}: {}", agent_name, result.stderr)
        return False

    return True


def _write_notification_event(events_dir: Path, message: str, level: str = "WARNING") -> None:
    """Write a notification event to events/delivery_failures/events.jsonl.

    These events are visible through the event system and web UI,
    providing user-facing notifications about delivery issues.
    """
    now = datetime.now(timezone.utc)
    event = {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z",
        "type": "delivery_notification",
        "event_id": f"evt-{uuid4().hex}",
        "source": "delivery_failures",
        "level": level,
        "message": message,
    }
    delivery_failures_dir = events_dir / "delivery_failures"
    delivery_failures_dir.mkdir(parents=True, exist_ok=True)
    events_file = delivery_failures_dir / "events.jsonl"
    try:
        with events_file.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:
        logger.error("Failed to write notification event: {}", exc)


_CHAT_NOTIFICATION_TIMEOUT_SECONDS: Final[float] = 30.0


def _get_system_notifications_conversation_id() -> str | None:
    """Read the system_notifications conversation ID from the changeling_conversations table.

    Looks for a conversation tagged with ``{"internal": "system_notifications"}``
    in the llm database at ``$LLM_USER_PATH/logs.db``.
    Falls back to None if the database or table does not exist.
    """
    llm_user_path = os.environ.get("LLM_USER_PATH", "")
    if not llm_user_path:
        logger.warning("LLM_USER_PATH not set, cannot look up system_notifications conversation")
        return None
    db_path = Path(llm_user_path) / "logs.db"

    if not db_path.is_file():
        logger.debug("LLM database not found at {}", db_path)
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT conversation_id FROM changeling_conversations "
                "WHERE json_extract(tags, '$.internal') = 'system_notifications'",
            ).fetchall()
            if rows:
                return str(rows[0][0])
        except sqlite3.Error as exc:
            logger.debug("Failed to query changeling_conversations: {}", exc)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Failed to read system_notifications conversation from DB: {}", exc)
    return None


def _send_chat_notification(events_dir: Path, message: str) -> bool:
    """Send a notification as a chat message via ``llm``.

    Uses the system_notifications conversation (found by tag in the
    changeling_conversations table) so that all system notifications appear
    in the same thread. The message is sent as the user prompt; the model
    response is discarded.

    Returns True on success, False if ``llm`` is not available or fails.
    This is best-effort: the caller should not depend on success.
    """
    conversation_id = _get_system_notifications_conversation_id()
    if conversation_id is None:
        logger.warning("No system_notifications conversation found, skipping chat notification")
        return False

    try:
        result = subprocess.run(
            [
                "llm",
                "chat",
                "--cid",
                conversation_id,
                "-m",
                "matched-responses",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=_CHAT_NOTIFICATION_TIMEOUT_SECONDS,
            env={**os.environ, "LLM_MATCHED_RESPONSE": ""},
        )
        if result.returncode == 0:
            logger.info("Sent chat notification via llm")
            return True
        logger.warning("llm chat returned non-zero: {}", result.stderr)
    except FileNotFoundError:
        logger.warning("llm command not found, skipping chat notification")
    except subprocess.TimeoutExpired:
        logger.warning("Timed out sending chat notification via llm")
    except OSError as exc:
        logger.warning("Failed to invoke llm for chat notification: {}", exc)
    return False


def _notify_user(events_dir: Path, message: str, level: str = "WARNING") -> None:
    """Notify the user about a delivery issue.

    Uses two mechanisms for reliability:
    1. Writes a structured event to events/delivery_failures/events.jsonl (always persisted)
    2. Sends a chat message via ``llm`` (best-effort, visible in chat interface)
    """
    _write_notification_event(events_dir, message, level=level)
    _send_chat_notification(events_dir, message)


def _compute_backoff_seconds(consecutive_failures: int) -> float:
    """Compute exponential backoff duration based on the number of consecutive failures."""
    return min(_BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)), _BACKOFF_MAX_SECONDS)


# -- Subprocess management --


def _start_events_subprocess(agent_name: str, cel_filter: str) -> subprocess.Popen[str]:
    """Start ``mng events <agent_name> --follow --filter <cel_filter>`` as a subprocess."""
    cmd = [*get_mng_command(), "events", agent_name, "--follow"]
    if cel_filter:
        cmd.extend(["--filter", cel_filter])
    logger.info("Starting events subprocess: {}", " ".join(cmd))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _read_events_from_subprocess(
    process: subprocess.Popen[str],
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Read JSONL lines from subprocess stdout into the event buffer (thread target)."""
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if stop_event.is_set():
                break
            stripped = line.strip()
            if not stripped:
                continue
            with buffer_lock:
                event_buffer.append(stripped)
    except Exception as exc:
        if not stop_event.is_set():
            logger.error("Error reading from events subprocess: {}", exc)


def _drain_stderr(
    process: subprocess.Popen[str],
    stop_event: threading.Event,
) -> None:
    """Read and log stderr from the subprocess (daemon thread target)."""
    assert process.stderr is not None
    try:
        for line in process.stderr:
            if stop_event.is_set():
                break
            stripped = line.strip()
            if stripped:
                logger.warning("mng events stderr: {}", stripped)
    except Exception as exc:
        if not stop_event.is_set():
            logger.debug("Stderr reader error: {}", exc)


# -- Chat event pairing --

# Maximum time to wait for an assistant response before delivering the user
# message anyway (in seconds). Prevents events from being held indefinitely
# if the assistant never responds (e.g. conversation closed, error, etc.).
_CHAT_PAIR_TIMEOUT_SECONDS: Final[float] = 300.0


def _separate_chat_events(
    lines: list[str],
    held_user_messages: dict[str, tuple[list[str], float]],
) -> list[str]:
    """Separate chat message events into paired (ready) and held (waiting).

    For events from the "messages" source:
    - User messages are held back until an assistant response for the same
      conversation_id arrives, so that the agent sees both together.
    - When an assistant message arrives, the corresponding held user messages
      are released and included alongside it.
    - User messages held longer than _CHAT_PAIR_TIMEOUT_SECONDS are released
      even without an assistant response.

    Non-message events pass through unchanged.

    Args:
        lines: Parsed JSONL lines to process.
        held_user_messages: Mutable dict mapping conversation_id to
            (list of held JSONL lines, timestamp when first held).
            Updated in place.

    Returns:
        Lines ready for delivery (non-message events + paired chat events).
    """
    ready: list[str] = []
    new_user_messages: dict[str, list[str]] = {}
    new_assistant_messages: dict[str, list[str]] = {}

    now = time.monotonic()

    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            ready.append(line)
            continue

        source = parsed.get("source", "")
        if source != "messages":
            ready.append(line)
            continue

        role = parsed.get("role", "")
        conversation_id = parsed.get("conversation_id", "")

        if role == "user" and conversation_id:
            new_user_messages.setdefault(conversation_id, []).append(line)
        elif role == "assistant" and conversation_id:
            new_assistant_messages.setdefault(conversation_id, []).append(line)
        else:
            ready.append(line)

    # Release paired messages: user messages first, then assistant messages
    for conversation_id, assistant_lines in new_assistant_messages.items():
        # Release any previously held user messages for this conversation
        if conversation_id in held_user_messages:
            held_lines, _ = held_user_messages.pop(conversation_id)
            ready.extend(held_lines)
        # Release new user messages for this conversation
        if conversation_id in new_user_messages:
            ready.extend(new_user_messages.pop(conversation_id))
        # Then the assistant messages
        ready.extend(assistant_lines)

    # Release timed-out held messages
    timed_out_conversation_ids = [
        conversation_id
        for conversation_id, (_, held_at) in held_user_messages.items()
        if now - held_at > _CHAT_PAIR_TIMEOUT_SECONDS
    ]
    for conversation_id in timed_out_conversation_ids:
        held_lines, _ = held_user_messages.pop(conversation_id)
        ready.extend(held_lines)

    # Hold new user messages that don't have a matching assistant response
    for conversation_id, user_lines in new_user_messages.items():
        if conversation_id in held_user_messages:
            held_user_messages[conversation_id][0].extend(user_lines)
        else:
            held_user_messages[conversation_id] = (user_lines, now)

    return ready


# -- Delivery loop helpers --


def _filter_catchup_events(
    pending: list[str],
    delivery_state: _DeliveryState,
    is_catching_up: bool,
) -> tuple[list[str], dict[str, Any], bool]:
    """Parse JSONL lines and filter out already-delivered events during catch-up.

    Returns (deliverable_lines, last_parsed_event, is_still_catching_up).
    """
    deliverable_lines: list[str] = []
    last_parsed: dict[str, Any] = {}

    for line in pending:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSONL line: {}", line[:200])
            continue

        if is_catching_up and _should_skip_for_catchup(parsed, delivery_state):
            continue

        # Once we see a non-skipped event, catch-up is done
        is_catching_up = False
        deliverable_lines.append(line)
        last_parsed = parsed

    return deliverable_lines, last_parsed, is_catching_up


def _compute_rate_warning(
    rate_tracker: _SendRateTracker,
    threshold: int,
) -> str | None:
    """Return a rate warning string if the send rate exceeds the threshold."""
    current_rate = rate_tracker.messages_per_minute()
    if current_rate >= threshold:
        return f"High event rate: {current_rate:.0f} messages/min (threshold: {threshold}/min)"
    return None


def _deliver_batch(
    deliverable_lines: list[str],
    last_parsed: dict[str, Any],
    agent_name: str,
    delivery_state: _DeliveryState,
    state_file: Path,
    rate_tracker: _SendRateTracker,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    time_since_last: float | None,
    rate_warning: str | None,
) -> bool:
    """Format, send, and persist a batch of events. Returns True on success.

    On failure, puts events back in the buffer for later retry.
    The caller is responsible for backoff and notification logic.
    """
    message = _format_delivery_message(deliverable_lines, time_since_last, rate_warning)
    logger.info("Sending {} event(s) to '{}'", len(deliverable_lines), agent_name)

    if _send_message(agent_name, message):
        rate_tracker.record_send()
        delivery_state.last_event_id = last_parsed.get("event_id", "")
        delivery_state.last_timestamp = last_parsed.get("timestamp", "")
        delivery_state.last_delivery_monotonic = time.monotonic()
        _save_delivery_state(state_file, delivery_state)
        logger.info("Delivered {} event(s), state updated", len(deliverable_lines))
        return True

    logger.warning("Failed to deliver {} event(s), will retry", len(deliverable_lines))
    with buffer_lock:
        event_buffer[0:0] = deliverable_lines
    return False


# -- Delivery loop --


def _run_delivery_loop(
    settings: _EventWatcherSettings,
    agent_name: str,
    state_file: Path,
    events_dir: Path,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Main delivery loop: drain buffer, rate-limit, format, and deliver to agent.

    Tracks consecutive delivery failures and applies exponential backoff.
    After ``max_delivery_retries`` consecutive failures, writes a notification
    event to events/monitor/events.jsonl. On recovery, writes another event.
    """
    delivery_state = _load_delivery_state(state_file)
    is_catching_up = bool(delivery_state.last_event_id or delivery_state.last_timestamp)

    if is_catching_up:
        logger.info(
            "Resuming from last delivered event: {} ({})",
            delivery_state.last_event_id,
            delivery_state.last_timestamp,
        )

    token_bucket = _TokenBucket(
        burst_size=settings.burst_size,
        rate_per_second=settings.max_messages_per_minute / 60.0,
    )
    rate_tracker = _SendRateTracker()
    consecutive_failures = 0
    has_notified_user = False

    # Chat event pairing: hold user messages until assistant responds
    held_user_messages: dict[str, tuple[list[str], float]] = {}

    while not stop_event.is_set():
        # If we're in a failure state, wait with exponential backoff
        if consecutive_failures > 0:
            backoff = _compute_backoff_seconds(consecutive_failures)
            logger.debug("Backing off for {:.1f}s after {} consecutive failures", backoff, consecutive_failures)
            stop_event.wait(timeout=backoff)
            if stop_event.is_set():
                break

        # Wait for a token to become available
        wait_time = token_bucket.time_until_token()
        if wait_time > 0:
            stop_event.wait(timeout=min(wait_time, 1.0))
            if stop_event.is_set():
                break
            continue

        # Drain the buffer
        with buffer_lock:
            pending = list(event_buffer)
            event_buffer.clear()

        if not pending:
            # Even with no new events, check for timed-out held chat messages
            if held_user_messages:
                deliverable_lines = _separate_chat_events([], held_user_messages)
                if deliverable_lines:
                    last_parsed = {}
                    for line in deliverable_lines:
                        try:
                            last_parsed = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                else:
                    stop_event.wait(timeout=_DELIVERY_POLL_INTERVAL_SECONDS)
                    continue
            else:
                stop_event.wait(timeout=_DELIVERY_POLL_INTERVAL_SECONDS)
                continue
        else:
            # Parse and filter for catch-up
            deliverable_lines, last_parsed, is_catching_up = _filter_catchup_events(
                pending, delivery_state, is_catching_up
            )

            # Separate chat events: hold user messages until assistant responds
            deliverable_lines = _separate_chat_events(deliverable_lines, held_user_messages)

        if not deliverable_lines:
            continue

        # Recompute last_parsed: find the chronologically latest event since
        # chat separation may have reordered lines (user messages before assistant)
        # or released held messages from earlier batches.
        latest_ts = ""
        for line in deliverable_lines:
            try:
                parsed = json.loads(line)
                ts = parsed.get("timestamp", "")
                if ts >= latest_ts:
                    latest_ts = ts
                    last_parsed = parsed
            except json.JSONDecodeError:
                continue

        # Consume a rate-limit token
        if not token_bucket.consume():
            with buffer_lock:
                event_buffer[0:0] = deliverable_lines
            continue

        # Compute delivery metadata
        time_since_last: float | None = None
        if delivery_state.last_delivery_monotonic > 0:
            time_since_last = time.monotonic() - delivery_state.last_delivery_monotonic

        rate_warning = _compute_rate_warning(rate_tracker, settings.high_rate_warning_threshold)

        # Send the batch (single attempt, no retries inside)
        success = _deliver_batch(
            deliverable_lines=deliverable_lines,
            last_parsed=last_parsed,
            agent_name=agent_name,
            delivery_state=delivery_state,
            state_file=state_file,
            rate_tracker=rate_tracker,
            event_buffer=event_buffer,
            buffer_lock=buffer_lock,
            time_since_last=time_since_last,
            rate_warning=rate_warning,
        )

        if success:
            if has_notified_user:
                _notify_user(
                    events_dir,
                    f"Event delivery to agent '{agent_name}' has recovered "
                    f"after {consecutive_failures} consecutive failures.",
                    level="INFO",
                )
                logger.info("Event delivery recovered after {} failures", consecutive_failures)
            consecutive_failures = 0
            has_notified_user = False
        else:
            consecutive_failures += 1
            logger.warning(
                "Delivery failure {} for agent '{}'",
                consecutive_failures,
                agent_name,
            )
            if consecutive_failures >= settings.max_delivery_retries and not has_notified_user:
                _notify_user(
                    events_dir,
                    f"Event delivery to agent '{agent_name}' has failed "
                    f"{consecutive_failures} consecutive times. "
                    "Events are being buffered and will be retried.",
                    level="ERROR",
                )
                logger.error(
                    "Delivery has failed {} consecutive times, notified user",
                    consecutive_failures,
                )
                has_notified_user = True


# -- Main --


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_work_dir = Path(require_env("MNG_AGENT_WORK_DIR"))
    agent_name = require_env("MNG_AGENT_NAME")
    host_dir = Path(require_env("MNG_HOST_DIR"))

    setup_watcher_logging("event_watcher", host_dir / "events" / "logs")

    settings = _load_watcher_settings(agent_work_dir)

    # Delivery state persistence
    state_dir = agent_state_dir / "events"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / _DELIVERY_STATE_FILENAME

    # Events directory for notification events
    events_dir = agent_state_dir / "events"

    logger.info("Event watcher started")
    logger.info("  Agent name: {}", agent_name)
    logger.info("  CEL filter: {}", settings.cel_filter)
    logger.info("  Burst size: {}", settings.burst_size)
    logger.info("  Max messages/min: {}", settings.max_messages_per_minute)
    logger.info("  Rate warning threshold: {}/min", settings.high_rate_warning_threshold)
    logger.info("  Max delivery retries: {}", settings.max_delivery_retries)
    logger.info("  State file: {}", state_file)

    stop_event = threading.Event()
    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    active_process: subprocess.Popen[str] | None = None

    # Start the long-lived delivery thread
    delivery_thread = threading.Thread(
        target=_run_delivery_loop,
        args=(settings, agent_name, state_file, events_dir, event_buffer, buffer_lock, stop_event),
        daemon=True,
    )
    delivery_thread.start()

    try:
        while not stop_event.is_set():
            active_process = _start_events_subprocess(agent_name, settings.cel_filter)

            # Reader thread feeds subprocess stdout into the shared buffer
            reader_thread = threading.Thread(
                target=_read_events_from_subprocess,
                args=(active_process, event_buffer, buffer_lock, stop_event),
                daemon=True,
            )
            reader_thread.start()

            # Stderr drain thread
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(active_process, stop_event),
                daemon=True,
            )
            stderr_thread.start()

            # Wait for subprocess to exit
            active_process.wait()
            logger.warning("mng events subprocess exited with code {}", active_process.returncode)
            active_process = None

            if stop_event.is_set():
                break

            logger.info("Restarting events subprocess in {}s", _SUBPROCESS_RESTART_DELAY_SECONDS)
            stop_event.wait(timeout=_SUBPROCESS_RESTART_DELAY_SECONDS)

    except KeyboardInterrupt:
        logger.info("Event watcher stopping (KeyboardInterrupt)")
    finally:
        stop_event.set()
        if active_process is not None and active_process.poll() is None:
            active_process.terminate()
            try:
                active_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                active_process.kill()


if __name__ == "__main__":
    main()
