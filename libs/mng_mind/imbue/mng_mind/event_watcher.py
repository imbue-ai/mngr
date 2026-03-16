#!/usr/bin/env python3
"""Event watcher for mind agents.

Streams events from ``mng events --follow --filter`` and delivers them
to the primary agent via ``mng message`` with debouncing and rate limiting.

Events are batched and written to JSONL files under
``$AGENT_STATE_DIR/mind/event_batches/<uuid>.jsonl``, and the agent
receives a message pointing to that file rather than the events
themselves.

The watcher delegates all event discovery, deduplication, and filtering
to the ``mng events`` command (run as a subprocess). This script handles:

- At-least-once delivery with minimal duplicates on restart
- Token-bucket rate limiting (burst + sustained rate)
- File-based event delivery (events written to event_batches/)
- Subprocess lifecycle (restart on exit)
- Synthetic event generation:
  - ``mind/idle``: periodic idle notifications when no events arrive
  - ``mind/schedule``: time-of-day events in the user's timezone
  - ``mind/onboarding``: one-time event on first run

Usage: mng mind-event-watcher

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
  MNG_AGENT_WORK_DIR   - agent working directory (contains minds.toml)
  MNG_AGENT_ID         - ID of the primary agent to send messages to
  ROLE                 - (optional) role name; ignored_sources.txt is read
                         from $MNG_AGENT_WORK_DIR/$ROLE/ignored_sources.txt
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4
from zoneinfo import ZoneInfo

from loguru import logger

from imbue.mng_recursive.watcher_common import DEFAULT_CEL_FILTER
from imbue.mng_recursive.watcher_common import MngNotInstalledError
from imbue.mng_recursive.watcher_common import get_mng_command
from imbue.mng_recursive.watcher_common import load_watchers_section
from imbue.mng_recursive.watcher_common import require_env
from imbue.mng_recursive.watcher_common import setup_watcher_logging

# -- Constants --
# NOTE: These defaults must be kept in sync with the Field defaults in
# data_types.py WatcherSettings. They are duplicated here because this
# script runs standalone on the host and cannot import from data_types.py.


_DEFAULT_BURST_SIZE: Final[int] = 5
_DEFAULT_MAX_MESSAGES_PER_MINUTE: Final[int] = 10
_DEFAULT_MAX_DELIVERY_RETRIES: Final[int] = 3
_DEFAULT_MAX_EVENT_LENGTH: Final[int] = 50_000
_DEFAULT_MAX_SAME_SOURCE_EVENTS_PER_BATCH: Final[int] = 20

_DELIVERY_STATE_FILENAME: Final[str] = ".event_delivery_state.json"

_SUBPROCESS_RESTART_DELAY_SECONDS: Final[float] = 5.0

_MESSAGE_SEND_TIMEOUT_SECONDS: Final[float] = 120.0

# How often the delivery loop polls for buffered events
_DELIVERY_POLL_INTERVAL_SECONDS: Final[float] = 0.5

# Exponential backoff for delivery retries
_BACKOFF_BASE_SECONDS: Final[float] = 2.0
_BACKOFF_MAX_SECONDS: Final[float] = 60.0

_IGNORED_SOURCES_FILENAME: Final[str] = "ignored_sources.txt"

# How often the synthetic events loop checks for idle/schedule/onboarding events
_SYNTHETIC_POLL_INTERVAL_SECONDS: Final[float] = 10.0

_ONBOARDING_MARKER_FILENAME: Final[str] = ".onboarding_sent"
_SCHEDULED_STATE_FILENAME: Final[str] = ".scheduled_events_state.json"

# Synthetic event source names (must match SOURCE_MIND_* in data_types.py)
_SOURCE_MIND_IDLE: Final[str] = "mind/idle"
_SOURCE_MIND_SCHEDULE: Final[str] = "mind/schedule"
_SOURCE_MIND_ONBOARDING: Final[str] = "mind/onboarding"


# -- Settings --


@dataclasses.dataclass(frozen=True)
class _EventWatcherSettings:
    """Parsed event watcher settings from settings.toml."""

    cel_filter: str = DEFAULT_CEL_FILTER
    event_exclude_sources: tuple[str, ...] = ()
    burst_size: int = _DEFAULT_BURST_SIZE
    max_messages_per_minute: int = _DEFAULT_MAX_MESSAGES_PER_MINUTE
    max_delivery_retries: int = _DEFAULT_MAX_DELIVERY_RETRIES
    max_event_length: int = _DEFAULT_MAX_EVENT_LENGTH
    max_same_source_events_per_batch: int = _DEFAULT_MAX_SAME_SOURCE_EVENTS_PER_BATCH
    idle_event_delay_minutes_schedule: tuple[int, ...] = ()
    scheduled_events: tuple[tuple[str, str], ...] = ()
    user_timezone: str = "UTC"


def _load_watcher_settings(agent_work_dir: Path) -> _EventWatcherSettings:
    """Load event watcher settings from settings.toml, falling back to defaults."""
    watchers = load_watchers_section(agent_work_dir)
    if not watchers:
        return _EventWatcherSettings()
    raw_scheduled = watchers.get("scheduled_events", {})
    return _EventWatcherSettings(
        cel_filter=watchers.get("event_cel_filter", DEFAULT_CEL_FILTER),
        event_exclude_sources=tuple(watchers.get("event_exclude_sources", ())),
        burst_size=watchers.get("event_burst_size", _DEFAULT_BURST_SIZE),
        max_messages_per_minute=watchers.get("max_event_messages_per_minute", _DEFAULT_MAX_MESSAGES_PER_MINUTE),
        max_delivery_retries=watchers.get("max_delivery_retries", _DEFAULT_MAX_DELIVERY_RETRIES),
        max_event_length=watchers.get("max_event_length", _DEFAULT_MAX_EVENT_LENGTH),
        max_same_source_events_per_batch=watchers.get(
            "max_same_source_events_per_batch", _DEFAULT_MAX_SAME_SOURCE_EVENTS_PER_BATCH
        ),
        idle_event_delay_minutes_schedule=tuple(watchers.get("idle_event_delay_minutes_schedule", ())),
        scheduled_events=tuple((k, v) for k, v in raw_scheduled.items()),
        user_timezone=watchers.get("user_timezone", "UTC"),
    )


# -- Ignored sources --


@dataclasses.dataclass
class _IgnoredSourcesState:
    """Tracks the ignored_sources.txt file and its cached contents."""

    file_path: Path
    last_mtime: float = 0.0
    ignored_sources: frozenset[str] = frozenset()


def _load_ignored_sources_if_updated(state: _IgnoredSourcesState) -> frozenset[str]:
    """Re-read ignored_sources.txt if the file has been modified since last read.

    Returns the current set of ignored sources (may be empty if the file
    does not exist or is empty).
    """
    try:
        current_mtime = state.file_path.stat().st_mtime
    except OSError:
        if state.ignored_sources:
            logger.debug("ignored_sources.txt no longer exists, clearing ignored sources")
            state.ignored_sources = frozenset()
            state.last_mtime = 0.0
        return state.ignored_sources

    if current_mtime == state.last_mtime:
        return state.ignored_sources

    try:
        content = state.file_path.read_text()
    except OSError as exc:
        logger.warning("Failed to read {}: {}", state.file_path, exc)
        return state.ignored_sources

    sources = frozenset(
        line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")
    )
    if sources != state.ignored_sources:
        logger.info("Updated ignored sources from {}: {}", state.file_path, sorted(sources))
    state.ignored_sources = sources
    state.last_mtime = current_mtime
    return sources


def _filter_ignored_sources(lines: list[str], ignored_sources: frozenset[str]) -> list[str]:
    """Remove events whose source is in the ignored set."""
    if not ignored_sources:
        return lines
    result: list[str] = []
    for line in lines:
        try:
            parsed = json.loads(line)
            source = parsed.get("source", "")
        except json.JSONDecodeError:
            result.append(line)
            continue
        if source not in ignored_sources:
            result.append(line)
    return result


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


# -- Message sending --


def _send_message(agent_id: str, message: str) -> bool:
    """Send a message to the agent via mng message. Returns True on success."""
    try:
        result = subprocess.run(
            [*get_mng_command(), "message", agent_id, "--provider", "local", "-m", message],
            capture_output=True,
            text=True,
            timeout=_MESSAGE_SEND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out sending message to {}", agent_id)
        return False
    except (OSError, MngNotInstalledError) as exc:
        logger.error("Failed to invoke mng message subprocess: {}", exc)
        return False

    if result.returncode != 0:
        logger.error("mng message returned non-zero for {}: {}", agent_id, result.stderr)
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
    """Read the system_notifications conversation ID from the mind_conversations table.

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
                "SELECT conversation_id FROM mind_conversations "
                "WHERE json_extract(tags, '$.internal') = 'system_notifications'",
            ).fetchall()
            if rows:
                return str(rows[0][0])
        except sqlite3.Error as exc:
            logger.debug("Failed to query mind_conversations: {}", exc)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Failed to read system_notifications conversation from DB: {}", exc)
    return None


def _send_chat_notification(events_dir: Path, message: str) -> bool:
    """Send a notification as a chat message via ``llm prompt``.

    Uses the system_notifications conversation (found by tag in the
    mind_conversations table) so that all system notifications appear
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
                "prompt",
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
        logger.warning("llm prompt returned non-zero: {}", result.stderr)
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


def _start_events_subprocess(agent_id: str, cel_filter: str) -> subprocess.Popen[str]:
    """Start ``mng events <agent_id> --follow --filter <cel_filter>`` as a subprocess."""
    cmd = [*get_mng_command(), "events", agent_id, "--follow"]
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
    last_real_event_monotonic: list[float] | None = None,
) -> None:
    """Read JSONL lines from subprocess stdout into the event buffer (thread target).

    If last_real_event_monotonic is provided, updates it (under buffer_lock)
    whenever a real event is received, enabling idle detection.
    """
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
                if last_real_event_monotonic is not None:
                    last_real_event_monotonic[0] = time.monotonic()
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


# -- Synthetic event helpers --


class InvalidTimeFormatError(Exception):
    """Raised when a time-of-day string cannot be parsed."""


def _format_utc_timestamp_now() -> str:
    """Format the current UTC time as an ISO 8601 timestamp with nanosecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"


def _resolve_user_timezone(timezone_name: str) -> ZoneInfo:
    """Resolve a timezone name to a ZoneInfo object, falling back to UTC on error."""
    try:
        return ZoneInfo(timezone_name)
    except KeyError:
        logger.warning("Unknown timezone '{}', falling back to UTC", timezone_name)
        return ZoneInfo("UTC")


def _make_synthetic_event_line(
    event_type: str,
    source: str,
    extra_fields: dict[str, Any] | None = None,
) -> str:
    """Create a JSONL line for a synthetic event with standard envelope fields."""
    event: dict[str, Any] = {
        "timestamp": _format_utc_timestamp_now(),
        "type": event_type,
        "event_id": f"evt-{uuid4().hex}",
        "source": source,
    }
    if extra_fields:
        event.update(extra_fields)
    return json.dumps(event, separators=(",", ":"))


def _cumulative_idle_delay_minutes(schedule: tuple[int, ...], event_index: int) -> int:
    """Calculate the cumulative delay in minutes for the nth idle event (0-indexed).

    For schedule [1, 10, 60] and event_index=2, returns 1+10+60=71.
    For event_index >= len(schedule), the last value repeats.
    """
    total = 0
    for i in range(event_index + 1):
        if i < len(schedule):
            total += schedule[i]
        else:
            total += schedule[-1]
    return total


def _parse_time_of_day(time_str: str) -> tuple[int, int, int]:
    """Parse a time-of-day string like '13:37:30' or '15:00' into (hour, minute, second).

    Raises InvalidTimeFormatError if the format is invalid or contains
    non-numeric values.
    """
    parts = time_str.strip().split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise InvalidTimeFormatError(f"Invalid time format '{time_str}', expected HH:MM or HH:MM:SS")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise InvalidTimeFormatError(f"Non-numeric value in time '{time_str}'") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise InvalidTimeFormatError(f"Time values out of range in '{time_str}'")
    return hour, minute, second


def _load_scheduled_events_state(state_file: Path) -> tuple[str, set[str]]:
    """Load the scheduled events state: (date_str, set of fired event names).

    Returns ("", empty set) if the file doesn't exist or is corrupt.
    """
    try:
        if not state_file.is_file():
            return "", set()
        raw = json.loads(state_file.read_text())
        return raw.get("date", ""), set(raw.get("fired_events", []))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load scheduled events state: {}", exc)
        return "", set()


def _save_scheduled_events_state(state_file: Path, date_str: str, fired_events: set[str]) -> None:
    """Persist which scheduled events have fired today."""
    data = {"date": date_str, "fired_events": sorted(fired_events)}
    tmp_file = state_file.with_suffix(".tmp")
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(json.dumps(data))
        tmp_file.rename(state_file)
    except OSError as exc:
        logger.error("Failed to save scheduled events state: {}", exc)


def _maybe_send_onboarding(
    mind_state_dir: Path,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
) -> None:
    """Send a mind/onboarding event if none has ever been sent (marker file absent)."""
    onboarding_marker = mind_state_dir / _ONBOARDING_MARKER_FILENAME
    if onboarding_marker.exists():
        return
    logger.info("Sending mind/onboarding event (first run)")
    line = _make_synthetic_event_line("onboarding", _SOURCE_MIND_ONBOARDING)
    with buffer_lock:
        event_buffer.append(line)
    try:
        mind_state_dir.mkdir(parents=True, exist_ok=True)
        onboarding_marker.touch()
    except OSError as exc:
        logger.error("Failed to create onboarding marker: {}", exc)


def _maybe_send_idle_event(
    settings: _EventWatcherSettings,
    elapsed_minutes: float,
    idle_events_sent: int,
    user_tz: ZoneInfo,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
) -> int:
    """Send a mind/idle event if the idle threshold has been reached.

    Returns the updated idle_events_sent count.
    """
    cumulative_minutes = _cumulative_idle_delay_minutes(settings.idle_event_delay_minutes_schedule, idle_events_sent)
    if elapsed_minutes < cumulative_minutes:
        return idle_events_sent

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(user_tz)
    line = _make_synthetic_event_line(
        "idle",
        _SOURCE_MIND_IDLE,
        {
            "current_time_utc": now_utc.isoformat(),
            "current_time_local": now_local.isoformat(),
            "minutes_since_last_event": round(elapsed_minutes, 1),
            "idle_event_number": idle_events_sent + 1,
        },
    )
    logger.info(
        "Sending mind/idle event {} after {:.1f} minutes of inactivity",
        idle_events_sent + 1,
        elapsed_minutes,
    )
    with buffer_lock:
        event_buffer.append(line)
    return idle_events_sent + 1


def _check_scheduled_events(
    settings: _EventWatcherSettings,
    user_tz: ZoneInfo,
    saved_date: str,
    fired_today: set[str],
    scheduled_state_file: Path,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
) -> tuple[str, set[str]]:
    """Fire any scheduled events whose time has passed today.

    Returns the updated (saved_date, fired_today) tuple.
    """
    now_local = datetime.now(user_tz)
    today_str = now_local.strftime("%Y-%m-%d")

    if today_str != saved_date:
        fired_today = set()
        saved_date = today_str

    for event_name, time_str in settings.scheduled_events:
        if event_name in fired_today:
            continue
        try:
            hour, minute, second = _parse_time_of_day(time_str)
        except InvalidTimeFormatError as exc:
            logger.warning("Skipping scheduled event '{}': {}", event_name, exc)
            fired_today.add(event_name)
            continue

        scheduled_time = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if now_local >= scheduled_time:
            now_utc = datetime.now(timezone.utc)
            line = _make_synthetic_event_line(
                "schedule",
                _SOURCE_MIND_SCHEDULE,
                {
                    "event_name": event_name,
                    "scheduled_time": time_str,
                    "current_time_utc": now_utc.isoformat(),
                    "current_time_local": now_local.isoformat(),
                },
            )
            logger.info("Sending mind/schedule event '{}' (scheduled for {})", event_name, time_str)
            with buffer_lock:
                event_buffer.append(line)
            fired_today.add(event_name)
            _save_scheduled_events_state(scheduled_state_file, today_str, fired_today)

    return saved_date, fired_today


def _run_synthetic_events_loop(
    settings: _EventWatcherSettings,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    stop_event: threading.Event,
    last_real_event_monotonic: list[float],
    mind_state_dir: Path,
    time_source: Callable[[], float] = time.monotonic,
    poll_interval_seconds: float = _SYNTHETIC_POLL_INTERVAL_SECONDS,
) -> None:
    """Generate synthetic events: idle, scheduled, and onboarding.

    Runs as a daemon thread alongside the delivery loop. Injects events
    directly into the shared event_buffer for delivery to the agent.
    """
    user_tz = _resolve_user_timezone(settings.user_timezone)
    _maybe_send_onboarding(mind_state_dir, event_buffer, buffer_lock)

    idle_events_sent = 0
    last_seen_real_event_time = last_real_event_monotonic[0]

    scheduled_state_file = mind_state_dir / _SCHEDULED_STATE_FILENAME
    saved_date, fired_today = _load_scheduled_events_state(scheduled_state_file)

    while not stop_event.is_set():
        stop_event.wait(timeout=poll_interval_seconds)
        if stop_event.is_set():
            break

        now_monotonic = time_source()

        with buffer_lock:
            current_real_event_time = last_real_event_monotonic[0]
        if current_real_event_time > last_seen_real_event_time:
            idle_events_sent = 0
            last_seen_real_event_time = current_real_event_time

        if settings.idle_event_delay_minutes_schedule:
            elapsed_minutes = (now_monotonic - last_seen_real_event_time) / 60.0
            idle_events_sent = _maybe_send_idle_event(
                settings,
                elapsed_minutes,
                idle_events_sent,
                user_tz,
                event_buffer,
                buffer_lock,
            )

        if settings.scheduled_events:
            saved_date, fired_today = _check_scheduled_events(
                settings,
                user_tz,
                saved_date,
                fired_today,
                scheduled_state_file,
                event_buffer,
                buffer_lock,
            )


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


def _write_events_file(event_lines: list[str], directory: Path) -> Path | None:
    """Write event lines to a JSONL file in the given directory.

    Returns the file path on success, or None on failure.
    """
    file_path = directory / f"{uuid4().hex}.jsonl"
    try:
        with open(file_path, "w") as f:
            for line in event_lines:
                f.write(line + "\n")
        return file_path
    except OSError as exc:
        logger.error("Failed to write events file {}: {}", file_path, exc)
        return None


def _apply_special_event_handling(
    deliverable_lines: list[str],
    event_lists_dir: Path,
    max_event_length: int,
    max_same_source_events_per_batch: int,
) -> list[str]:
    """Apply aggregation to event lines that exceed configured limits.

    Two conditions trigger aggregation for all events from a given source:
    1. Any single event line from that source exceeds ``max_event_length`` characters.
    2. The source has more than ``max_same_source_events_per_batch`` events in the batch.

    When triggered, all events from that source are written to a JSONL file
    under ``event_lists_dir`` and replaced by a single aggregate event whose
    ``aggregate_events`` field contains the file path.
    """
    parsed_items: list[tuple[str, str]] = []
    lines_by_source: dict[str, list[str]] = {}
    max_ts_by_source: dict[str, str] = {}

    for line in deliverable_lines:
        try:
            parsed = json.loads(line)
            source = parsed.get("source", "")
            ts = parsed.get("timestamp", "")
        except json.JSONDecodeError:
            parsed_items.append((line, ""))
            continue
        parsed_items.append((line, source))
        if source:
            lines_by_source.setdefault(source, []).append(line)
            if ts > max_ts_by_source.get(source, ""):
                max_ts_by_source[source] = ts

    sources_to_aggregate: set[str] = set()
    for source, source_lines in lines_by_source.items():
        if len(source_lines) > max_same_source_events_per_batch:
            sources_to_aggregate.add(source)
            continue
        for source_line in source_lines:
            if len(source_line) > max_event_length:
                sources_to_aggregate.add(source)
                break

    if not sources_to_aggregate:
        return deliverable_lines

    aggregate_replacements: dict[str, str] = {}
    for source in list(sources_to_aggregate):
        source_lines = lines_by_source[source]
        aggregate_file = _write_events_file(source_lines, event_lists_dir)
        if aggregate_file is None:
            logger.warning("Failed to write aggregate file for source '{}', including events inline", source)
            sources_to_aggregate.discard(source)
            continue

        ts = max_ts_by_source.get(source, "")
        if not ts:
            now = datetime.now(timezone.utc)
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"

        aggregate_event = {
            "timestamp": ts,
            "type": "aggregate",
            "event_id": f"evt-{uuid4().hex}",
            "source": source,
            "aggregate_events": [str(aggregate_file)],
        }
        aggregate_replacements[source] = json.dumps(aggregate_event, separators=(",", ":"))

    if not sources_to_aggregate:
        return deliverable_lines

    result: list[str] = []
    inserted_sources: set[str] = set()
    for line, source in parsed_items:
        if source in sources_to_aggregate:
            if source not in inserted_sources:
                result.append(aggregate_replacements[source])
                inserted_sources.add(source)
        else:
            result.append(line)

    return result


def _deliver_batch(
    deliverable_lines: list[str],
    last_parsed: dict[str, Any],
    agent_id: str,
    delivery_state: _DeliveryState,
    state_file: Path,
    rate_tracker: _SendRateTracker,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    event_batches_dir: Path,
    send_message: Callable[[str, str], bool] = _send_message,
) -> bool:
    """Write events to a file and send the file path to the agent. Returns True on success.

    Events are written to ``event_batches_dir/<uuid>.jsonl``, then the
    agent receives a message pointing to that file.

    On failure, puts events back in the buffer for later retry and
    cleans up the orphaned events file.
    The caller is responsible for backoff and notification logic.
    """
    logger.info("Sending {} event(s) to '{}'", len(deliverable_lines), agent_id)

    events_file_path = _write_events_file(deliverable_lines, event_batches_dir)
    if events_file_path is None:
        logger.warning("Failed to write events file, will retry")
        with buffer_lock:
            event_buffer[0:0] = deliverable_lines
        return False

    message = f"Please process all events in {events_file_path}"

    if send_message(agent_id, message):
        rate_tracker.record_send()
        delivery_state.last_event_id = last_parsed.get("event_id", "")
        delivery_state.last_timestamp = last_parsed.get("timestamp", "")
        delivery_state.last_delivery_monotonic = time.monotonic()
        _save_delivery_state(state_file, delivery_state)
        logger.info("Delivered {} event(s) via {}, state updated", len(deliverable_lines), events_file_path)
        return True

    logger.warning("Failed to deliver {} event(s), will retry", len(deliverable_lines))
    try:
        events_file_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Failed to clean up events file {}: {}", events_file_path, exc)
    with buffer_lock:
        event_buffer[0:0] = deliverable_lines
    return False


# -- Delivery loop --


def _run_delivery_loop(
    settings: _EventWatcherSettings,
    agent_id: str,
    state_file: Path,
    events_dir: Path,
    event_buffer: list[str],
    buffer_lock: threading.Lock,
    stop_event: threading.Event,
    event_batches_dir: Path,
    event_lists_dir: Path,
    ignored_sources_state: _IgnoredSourcesState,
    send_message: Callable[[str, str], bool] = _send_message,
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

        # Apply special event handling (aggregation for oversized or too-many events)
        deliverable_lines = _apply_special_event_handling(
            deliverable_lines,
            event_lists_dir,
            settings.max_event_length,
            settings.max_same_source_events_per_batch,
        )

        # Filter out events from excluded and dynamically ignored sources
        ignored_sources = _load_ignored_sources_if_updated(ignored_sources_state)
        all_excluded = ignored_sources | frozenset(settings.event_exclude_sources)
        deliverable_lines = _filter_ignored_sources(deliverable_lines, all_excluded)

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

        # Send the batch (single attempt, no retries inside)
        success = _deliver_batch(
            deliverable_lines=deliverable_lines,
            last_parsed=last_parsed,
            agent_id=agent_id,
            delivery_state=delivery_state,
            state_file=state_file,
            rate_tracker=rate_tracker,
            event_buffer=event_buffer,
            buffer_lock=buffer_lock,
            event_batches_dir=event_batches_dir,
            send_message=send_message,
        )

        if success:
            if has_notified_user:
                _notify_user(
                    events_dir,
                    f"Event delivery to agent '{agent_id}' has recovered "
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
                agent_id,
            )
            if consecutive_failures >= settings.max_delivery_retries and not has_notified_user:
                _notify_user(
                    events_dir,
                    f"Event delivery to agent '{agent_id}' has failed "
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


def main(
    start_subprocess: Callable[[str, str], Any] = _start_events_subprocess,
    stop_event: threading.Event | None = None,
    send_message: Callable[[str, str], bool] = _send_message,
) -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_work_dir = Path(require_env("MNG_AGENT_WORK_DIR"))
    agent_id = require_env("MNG_AGENT_ID")

    setup_watcher_logging("event_watcher", agent_state_dir / "events" / "logs")

    settings = _load_watcher_settings(agent_work_dir)

    # Delivery state persistence
    state_dir = agent_state_dir / "events"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / _DELIVERY_STATE_FILENAME

    # Events directory for notification events
    events_dir = agent_state_dir / "events"

    # Directory for event batch files
    event_batches_dir = agent_state_dir / "mind" / "event_batches"
    event_batches_dir.mkdir(parents=True, exist_ok=True)

    # Directory for aggregated event list files
    event_lists_dir = agent_state_dir / "mind" / "event_lists"
    event_lists_dir.mkdir(parents=True, exist_ok=True)

    if stop_event is None:
        stop_event = threading.Event()

    logger.info("Event watcher started")
    logger.info("  Agent ID: {}", agent_id)
    logger.info("  CEL filter: {}", settings.cel_filter)
    logger.info("  Exclude sources: {}", settings.event_exclude_sources)
    logger.info("  Burst size: {}", settings.burst_size)
    logger.info("  Max messages/min: {}", settings.max_messages_per_minute)
    logger.info("  Max delivery retries: {}", settings.max_delivery_retries)
    logger.info("  Max event length: {}", settings.max_event_length)
    logger.info("  Max same-source events/batch: {}", settings.max_same_source_events_per_batch)
    logger.info("  State file: {}", state_file)
    logger.info("  Event batches dir: {}", event_batches_dir)
    logger.info("  Event lists dir: {}", event_lists_dir)
    logger.info("  Idle schedule: {}", settings.idle_event_delay_minutes_schedule)
    logger.info("  Scheduled events: {}", settings.scheduled_events)
    logger.info("  User timezone: {}", settings.user_timezone)

    # Resolve the ignored_sources.txt path: $MNG_AGENT_WORK_DIR/$ROLE/ignored_sources.txt
    role = os.environ.get("ROLE", "")
    if role:
        ignored_sources_path = agent_work_dir / role / _IGNORED_SOURCES_FILENAME
    else:
        ignored_sources_path = agent_work_dir / _IGNORED_SOURCES_FILENAME
    logger.info("  Ignored sources file: {}", ignored_sources_path)
    ignored_sources_state = _IgnoredSourcesState(file_path=ignored_sources_path)

    event_buffer: list[str] = []
    buffer_lock = threading.Lock()
    active_process: subprocess.Popen[str] | None = None

    # Shared monotonic timestamp of the last real event from the subprocess.
    # Updated by the reader thread, read by the synthetic events thread.
    last_real_event_monotonic: list[float] = [time.monotonic()]

    # Directory for synthetic event state files (onboarding marker, scheduled state)
    mind_state_dir = agent_state_dir / "mind"
    mind_state_dir.mkdir(parents=True, exist_ok=True)

    # Start the synthetic events thread (idle, scheduled, onboarding)
    synthetic_thread = threading.Thread(
        target=_run_synthetic_events_loop,
        args=(
            settings,
            event_buffer,
            buffer_lock,
            stop_event,
            last_real_event_monotonic,
            mind_state_dir,
        ),
        daemon=True,
    )
    synthetic_thread.start()

    # Start the long-lived delivery thread
    delivery_thread = threading.Thread(
        target=_run_delivery_loop,
        args=(
            settings,
            agent_id,
            state_file,
            events_dir,
            event_buffer,
            buffer_lock,
            stop_event,
            event_batches_dir,
            event_lists_dir,
            ignored_sources_state,
            send_message,
        ),
        daemon=True,
    )
    delivery_thread.start()

    try:
        while not stop_event.is_set():
            active_process = start_subprocess(agent_id, settings.cel_filter)

            # Reader thread feeds subprocess stdout into the shared buffer
            reader_thread = threading.Thread(
                target=_read_events_from_subprocess,
                args=(active_process, event_buffer, buffer_lock, stop_event, last_real_event_monotonic),
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
