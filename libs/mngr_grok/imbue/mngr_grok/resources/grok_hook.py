#!/usr/bin/env python3
"""mngr's lifecycle hook handler for grok, run from ``$MNGR_AGENT_STATE_DIR/plugin/grok/home/hooks/``.

Invocations (each exits 0 with nothing on stdout or stderr, and logs one line to ``logs/grok_hooks.log``):

- ``grok_hook.py``: hook mode, the payload JSON on stdin; does nothing when ``GROK_AGENT=1`` (a nested grok).
- ``grok_hook.py --turn-completed <sessionId> <promptId>``: the transcript streamer saw that turn's ``turn_completed``.
- ``grok_hook.py --turn-dropped <sessionId> <promptId>``: mngr ends that turn, which grok dropped at mngr's Ctrl+C
  without reporting its end.

Both end the turn only while it is the session's recorded turn.

``active`` exists iff ``plugin/grok/turns/`` holds a file or ``plugin/grok/subagents/`` holds one not marked
``background``; a task a ``Stop`` lists in flight holds a work lease, ``leases/task-<id>``, instead. Every state change
runs under ``fcntl.flock`` on ``plugin/grok/state.lock``; a recompute that flips ``active`` appends a ``mngr/activity``
event to ``$MNGR_HOST_DIR/events/mngr/activity/events.jsonl``, and one that removes it writes ``idle_since``.

``permissions_waiting`` holds the session ids grok asked the user about, one per line (empty when a notification named
none); a tool of one of those sessions resolving, its top-level turn starting or ending, or the session ending removes
only that session's line, so a line outlives an approved permission card until its tool resolves. A top-level turn
ending while no subagent runs, or no turn or subagent remaining, removes the whole marker.

Files under ``$MNGR_AGENT_STATE_DIR/plugin/grok``:

- ``turns/<sessionId>``: ``<promptId>\\n<tail>\\n``, a top-level turn in flight; the promptId is
  ``pending-wake:<subagentId>`` while a finished background subagent's auto-wake turn is expected.
- ``submissions.log``: ``<promptId> <tail>\\n`` per top-level ``UserPromptSubmit``.
- ``subagents/<subagentId>``: ``<parent sessionId>\\n``, then ``background\\n`` once a parent ``Stop`` lists it.
- ``pending_wake/<parent sessionId>``: ``<subagentId>\\n``, a background subagent that ended during the parent's turn.
- ``session_id``, ``roots``, ``background_tasks.json``: the latest root session, every root session (one per line),
  and the latest root ``Stop{end_turn}``'s ``backgroundTasks``.

``<tail>`` is the normalized tail of a user prompt (its lowercased ASCII letters and digits, the last
``NORMALIZED_PROBE_MAX_LENGTH`` of them, possibly none), and ``AUTO_WAKE_TAIL_MARKER`` for an auto-wake turn
(promptId ``task-completed-<id>`` or ``subagent-completed-<id>``) or a pending-wake placeholder. No normalized tail
equals the marker.

Standard library only, compatible with Python 3.9.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import sys
import time
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import FrameType
from typing import Any
from typing import BinaryIO
from typing import Final
from typing import Optional

NORMALIZED_PROBE_MAX_LENGTH: Final[int] = 60
AUTO_WAKE_TAIL_MARKER: Final[str] = "<auto-wake>"
PENDING_WAKE_PROMPT_ID_PREFIX: Final[str] = "pending-wake:"
SUBAGENT_WAKE_PROMPT_ID_PREFIX: Final[str] = "subagent-completed-"
_TASK_WAKE_PROMPT_ID_PREFIX: Final[str] = "task-completed-"
_BACKGROUND_SUBAGENT_MARK: Final[str] = "background"
_TURN_COMPLETED_FLAG: Final[str] = "--turn-completed"
TURN_DROPPED_FLAG: Final[str] = "--turn-dropped"
STATE_LOCK_TIMEOUT_SECONDS: Final[float] = 0.5
# The --turn-completed and --turn-dropped calls do not hold grok up, so they wait longer.
_COMMAND_STATE_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0
# The shortest delay that re-arms a timer; setitimer treats 0 as cancel.
_MINIMUM_TIMER_DELAY_SECONDS: Final = 0.001

_PLUGIN_DIR_PARTS: Final[tuple[str, ...]] = ("plugin", "grok")
_TURNS_DIRNAME: Final[str] = "turns"
_SUBAGENTS_DIRNAME: Final[str] = "subagents"
_PENDING_WAKE_DIRNAME: Final[str] = "pending_wake"
_SESSION_ID_FILENAME: Final[str] = "session_id"
_FORK_FROM_FILENAME: Final[str] = "fork_from"
_ROOTS_FILENAME: Final[str] = "roots"
SUBMISSIONS_LOG_FILENAME: Final[str] = "submissions.log"
_BACKGROUND_TASKS_FILENAME: Final[str] = "background_tasks.json"
_STATE_LOCK_FILENAME: Final[str] = "state.lock"
ACTIVE_MARKER_FILENAME: Final[str] = "active"
PERMISSIONS_WAITING_FILENAME: Final[str] = "permissions_waiting"
_IDLE_SINCE_FILENAME: Final[str] = "idle_since"
SESSION_STARTED_FILENAME: Final[str] = "grok_session_started"
_HOOK_LOG_PATH_PARTS: Final[tuple[str, ...]] = ("logs", "grok_hooks.log")
_ACTIVITY_EVENTS_PATH_PARTS: Final[tuple[str, ...]] = ("events", "mngr", "activity", "events.jsonl")
_STANDARD_STREAM_DESCRIPTORS: Final[tuple[int, ...]] = (1, 2)

_PERMISSION_NOTIFICATION_TYPES: Final[frozenset[str]] = frozenset({"permission_prompt", "elicitation_dialog"})
_IDLE_PROMPT_NOTIFICATION_TYPE: Final[str] = "idle_prompt"
_END_TURN_REASON: Final[str] = "end_turn"
_SUBAGENT_TASK_TYPE: Final[str] = "subagent"

# One work lease per in-flight background task, under the agent's own state dir, so an agent
# whose turn ended with work outstanding reads RUNNING_BACKGROUND rather than WAITING. The
# grammar and its reader live in imbue.mngr.hosts.work_leases, which this script cannot import:
# it runs from the agent's hook directory against the standard library alone. The shell half of
# the same protocol is mngr_work_lease_lib.sh, and
# ``test_a_leased_grok_task_parses_the_way_mngr_reads_it`` pins this writer to that reader.
WORK_LEASE_DIRNAME: Final[str] = "leases"
WORK_LEASE_NAME_PREFIX: Final[str] = "task-"
WORK_LEASE_TTL_SECONDS: Final[int] = 14400
_LEASE_NAME_DISALLOWED_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]")
# grok's task statuses, of which these end the work. A status this does not name is leased,
# because a lease withheld from live work is the error the state exists to prevent.
_TERMINAL_TASK_STATUSES: Final[frozenset[str]] = frozenset({"completed", "failed", "killed", "cancelled"})

# Session and subagent ids name files, so they must be plain file names.
SAFE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PROMPT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A\S{1,256}\Z")
_NON_ALPHANUMERIC_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")
_CAMEL_CASE_BOUNDARY_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
_LOG_FIELD_MAX_LENGTH: Final[int] = 160


class GrokHookError(Exception):
    """Base class for the hook handler's errors."""


class StateLockTimeoutError(GrokHookError, TimeoutError):
    """Raised when the state lock is not acquired within the invocation's lock timeout."""


class MalformedHookPayloadError(GrokHookError, ValueError):
    """Raised when the hook payload on stdin is not a JSON object."""


def normalized_prompt_tail(prompt: str) -> str:
    return _NON_ALPHANUMERIC_PATTERN.sub("", prompt.lower())[-NORMALIZED_PROBE_MAX_LENGTH:]


def _is_auto_wake_prompt_id(prompt_id: str) -> bool:
    return prompt_id.startswith(_TASK_WAKE_PROMPT_ID_PREFIX) or prompt_id.startswith(SUBAGENT_WAKE_PROMPT_ID_PREFIX)


def _utc_timestamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}000Z"


def _plugin_dir(state_dir: Path) -> Path:
    return state_dir.joinpath(*_PLUGIN_DIR_PARTS)


def _turn_path(state_dir: Path, session_id: str) -> Path:
    return _plugin_dir(state_dir) / _TURNS_DIRNAME / session_id


def _subagent_path(state_dir: Path, subagent_id: str) -> Path:
    return _plugin_dir(state_dir) / _SUBAGENTS_DIRNAME / subagent_id


def _pending_wake_path(state_dir: Path, session_id: str) -> Path:
    return _plugin_dir(state_dir) / _PENDING_WAKE_DIRNAME / session_id


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() != ""]


def _read_first_line(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    return text.split("\n", 1)[0].strip()


def _write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(text, encoding="utf-8")
    os.replace(temporary_path, path)


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab", buffering=0) as handle:
        handle.write(text.encode("utf-8"))


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _remove_file(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _visible_entry_names(directory: Path) -> list[str]:
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(name for name in names if not name.startswith("."))


def _payload_field_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _payload_text(payload: Mapping[str, Any], camel_key: str, snake_key: str) -> str:
    return _payload_field_text(payload, camel_key) or _payload_field_text(payload, snake_key)


def _payload_id(payload: Mapping[str, Any], camel_key: str, snake_key: str) -> Optional[str]:
    value = _payload_text(payload, camel_key, snake_key)
    return value if SAFE_ID_PATTERN.match(value) else None


def _payload_session_id(payload: Mapping[str, Any]) -> Optional[str]:
    return _payload_id(payload, "sessionId", "session_id")


def _payload_prompt_id(payload: Mapping[str, Any]) -> Optional[str]:
    value = _payload_text(payload, "promptId", "prompt_id")
    return value if _PROMPT_ID_PATTERN.match(value) else None


def _is_subagent_payload(payload: Mapping[str, Any]) -> bool:
    return _payload_text(payload, "subagentType", "subagent_type") != ""


def snake_case_event_name(event_name: str) -> str:
    """The name HOOK_EVENT_HANDLERS keys an event by: ``PostToolUseFailure`` -> ``post_tool_use_failure``."""
    return _CAMEL_CASE_BOUNDARY_PATTERN.sub("_", event_name).lower()


def _hook_event_name(payload: Mapping[str, Any], environ: Mapping[str, str]) -> str:
    raw_name = _payload_text(payload, "hookEventName", "hook_event_name") or environ.get("GROK_HOOK_EVENT", "")
    return snake_case_event_name(raw_name)


def _parse_payload(raw_payload: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_payload)
    except ValueError as error:
        raise MalformedHookPayloadError(f"hook payload is not JSON ({len(raw_payload)} bytes)") from error
    if not isinstance(payload, dict):
        raise MalformedHookPayloadError(f"hook payload is a JSON {type(payload).__name__}, not an object")
    return payload


def _raise_state_lock_timeout(signal_number: int, frame: Optional[FrameType]) -> None:
    raise StateLockTimeoutError("state lock not acquired within the lock timeout")


@contextmanager
def _state_lock_timeout_alarm(timeout_seconds: float) -> Generator[None, None, None]:
    previous_handler = signal.signal(signal.SIGALRM, _raise_state_lock_timeout)
    previous_delay, previous_interval = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    started_at = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_delay > 0:
            remaining_delay = max(previous_delay - (time.monotonic() - started_at), _MINIMUM_TIMER_DELAY_SECONDS)
            signal.setitimer(signal.ITIMER_REAL, remaining_delay, previous_interval)


@contextmanager
def _held_state_lock(state_dir: Path, timeout_seconds: float) -> Generator[None, None, None]:
    """Hold the exclusive state lock; the kernel releases it when the file closes or the process dies."""
    lock_path = _plugin_dir(state_dir) / _STATE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_file:
        with _state_lock_timeout_alarm(timeout_seconds):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def _append_activity_event(host_dir: str, now: datetime) -> str:
    if host_dir == "":
        return " activity-event:no-host-dir"
    event = {
        "source": "mngr/activity",
        "type": "activity",
        "event_id": "evt-" + os.urandom(16).hex(),
        "timestamp": _utc_timestamp(now),
    }
    _append_text(Path(host_dir).joinpath(*_ACTIVITY_EVENTS_PATH_PARTS), json.dumps(event) + "\n")
    return ""


def _recompute_active_marker(state_dir: Path, host_dir: str, now: datetime) -> str:
    plugin_dir = _plugin_dir(state_dir)
    turn_ids = _visible_entry_names(plugin_dir / _TURNS_DIRNAME)
    # The marker covers a turn, which a background subagent is not part of: that one holds its
    # agent through a work lease. A dialog outlives the marker, because a background subagent
    # can be the one waiting on the user, and being blocked on a person is not working.
    has_work = bool(turn_ids or _foreground_subagent_ids(state_dir))
    can_hold_a_dialog = bool(turn_ids or _visible_entry_names(plugin_dir / _SUBAGENTS_DIRNAME))
    active_path = state_dir / ACTIVE_MARKER_FILENAME
    was_active = active_path.exists()
    if has_work and not was_active:
        _touch(active_path)
        _remove_file(state_dir / _IDLE_SINCE_FILENAME)
        return " active:on" + _append_activity_event(host_dir, now)
    elif not has_work and was_active:
        _remove_file(active_path)
        if not can_hold_a_dialog:
            _remove_file(state_dir / PERMISSIONS_WAITING_FILENAME)
        _write_text_atomically(state_dir / _IDLE_SINCE_FILENAME, _utc_timestamp(now) + "\n")
        return " active:off" + _append_activity_event(host_dir, now)
    elif not has_work:
        if not can_hold_a_dialog:
            _remove_file(state_dir / PERMISSIONS_WAITING_FILENAME)
        return ""
    else:
        return ""


def _foreground_subagent_ids(state_dir: Path) -> list[str]:
    """The subagents running inside a turn, which hold the turn marker open.

    One a parent's Stop listed is background work: it holds its agent through a work lease
    instead, so the marker can come off and the agent read RUNNING_BACKGROUND.
    """
    plugin_dir = _plugin_dir(state_dir)
    foreground = []
    for subagent_id in _visible_entry_names(plugin_dir / _SUBAGENTS_DIRNAME):
        lines = _read_lines(_subagent_path(state_dir, subagent_id))
        if _BACKGROUND_SUBAGENT_MARK not in lines[1:]:
            foreground.append(subagent_id)
    return foreground


def _write_pending_wake_placeholder(state_dir: Path, parent_id: str, subagent_id: str) -> None:
    placeholder = f"{PENDING_WAKE_PROMPT_ID_PREFIX}{subagent_id}\n{AUTO_WAKE_TAIL_MARKER}\n"
    _write_text_atomically(_turn_path(state_dir, parent_id), placeholder)


def _complete_turn(state_dir: Path, host_dir: str, session_id: str, prompt_id: str, now: datetime) -> str:
    turn_path = _turn_path(state_dir, session_id)
    if _read_first_line(turn_path) != prompt_id:
        return "ignored:not-the-current-turn"
    pending_subagent_id = _read_first_line(_pending_wake_path(state_dir, session_id))
    if pending_subagent_id:
        _write_pending_wake_placeholder(state_dir, session_id, pending_subagent_id)
        action = "turn:end wake:placeholder"
    else:
        _remove_file(turn_path)
        action = "turn:end"
    if _visible_entry_names(_plugin_dir(state_dir) / _SUBAGENTS_DIRNAME):
        _clear_session_permission_waiting(state_dir, session_id)
    else:
        _remove_file(state_dir / PERMISSIONS_WAITING_FILENAME)
    return action + _recompute_active_marker(state_dir, host_dir, now)


def _is_known_root(state_dir: Path, session_id: str) -> bool:
    return session_id in _read_lines(_plugin_dir(state_dir) / _ROOTS_FILENAME)


def _expect_parent_wake(state_dir: Path, parent_id: str, subagent_id: str, is_background: bool) -> str:
    if not _is_known_root(state_dir, parent_id):
        return "wake:none-parent-not-a-root"
    parent_prompt_id = _read_first_line(_turn_path(state_dir, parent_id))
    if parent_prompt_id == SUBAGENT_WAKE_PROMPT_ID_PREFIX + subagent_id:
        return "wake:none-already-woken"
    elif parent_prompt_id is None or parent_prompt_id.startswith(PENDING_WAKE_PROMPT_ID_PREFIX):
        _write_pending_wake_placeholder(state_dir, parent_id, subagent_id)
        return "wake:placeholder"
    elif is_background:
        _write_text_atomically(_pending_wake_path(state_dir, parent_id), subagent_id + "\n")
        return "wake:pending"
    else:
        return "wake:none-foreground"


def _end_subagent_session(state_dir: Path, host_dir: str, subagent_id: str, now: datetime) -> str:
    subagent_path = _subagent_path(state_dir, subagent_id)
    lines = _read_lines(subagent_path)
    if not lines:
        return "ignored:unknown-subagent"
    wake = _expect_parent_wake(state_dir, lines[0], subagent_id, _BACKGROUND_SUBAGENT_MARK in lines[1:])
    _remove_file(subagent_path)
    _remove_file(_turn_path(state_dir, subagent_id))
    _remove_file(_pending_wake_path(state_dir, subagent_id))
    _drop_lease(state_dir, subagent_id)
    _clear_session_permission_waiting(state_dir, subagent_id)
    return f"subagent:end {wake}" + _recompute_active_marker(state_dir, host_dir, now)


def _remove_subagents_of_parent(state_dir: Path, parent_id: str) -> int:
    removed_count = 0
    for subagent_id in _visible_entry_names(_plugin_dir(state_dir) / _SUBAGENTS_DIRNAME):
        subagent_path = _subagent_path(state_dir, subagent_id)
        if _read_first_line(subagent_path) == parent_id and _remove_file(subagent_path):
            removed_count += 1
    return removed_count


def _end_root_session(state_dir: Path, host_dir: str, session_id: str, now: datetime) -> str:
    is_turn_removed = _remove_file(_turn_path(state_dir, session_id))
    _remove_file(_pending_wake_path(state_dir, session_id))
    removed_subagent_count = _remove_subagents_of_parent(state_dir, session_id)
    # The leases this session declared stood for work inside it.
    _write_work_leases(state_dir, [], now)
    _clear_session_permission_waiting(state_dir, session_id)
    outcome = f"root:end turn-removed={str(is_turn_removed).lower()} subagents-removed={removed_subagent_count}"
    return outcome + _recompute_active_marker(state_dir, host_dir, now)


def _running_subagent_ids(background_tasks: Sequence[Any]) -> frozenset[str]:
    return frozenset(
        task["id"]
        for task in background_tasks
        if isinstance(task, dict) and task.get("type") == _SUBAGENT_TASK_TYPE and isinstance(task.get("id"), str)
    )


def _lease_dir(state_dir: Path) -> Path:
    return state_dir / WORK_LEASE_DIRNAME


def _lease_name_of(task_id: str) -> str:
    return WORK_LEASE_NAME_PREFIX + _LEASE_NAME_DISALLOWED_PATTERN.sub("_", task_id)


def _drop_lease(state_dir: Path, task_id: str) -> None:
    """A unit of work that has ended stops holding its agent, whichever path ended it."""
    _remove_file(_lease_dir(state_dir) / _lease_name_of(task_id))


def _in_flight_task_ids(background_tasks: Sequence[Any]) -> list[str]:
    """Every background task still in flight, whatever its kind, named for its lease."""
    ids = []
    for task in background_tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or task["id"] == "":
            continue
        if str(task.get("status") or "") in _TERMINAL_TASK_STATUSES:
            continue
        ids.append(_lease_name_of(task["id"]))
    return ids


def _write_work_leases(state_dir: Path, background_tasks: Sequence[Any], now: datetime) -> str:
    """Rewrite this writer's whole lease set, so work that has finished stops counting.

    Only leases under this writer's own prefix are cleared: the directory belongs to the agent
    and anything may lease there, and another writer's lease expires on its own bound.
    """
    lease_dir = _lease_dir(state_dir)
    for lease_name in _visible_entry_names(lease_dir):
        if lease_name.startswith(WORK_LEASE_NAME_PREFIX):
            _remove_file(lease_dir / lease_name)
    lease_names = _in_flight_task_ids(background_tasks)
    if not lease_names:
        return " leases=0"
    expires_at = int(now.timestamp()) + WORK_LEASE_TTL_SECONDS
    for lease_name in lease_names:
        _write_text_atomically(lease_dir / lease_name, f"until:{expires_at}\n")
    return f" leases={len(lease_names)}"


def _reconcile_subagents_with_snapshot(state_dir: Path, parent_id: str, running_subagent_ids: frozenset[str]) -> str:
    """Drop the parent's subagents absent from its Stop snapshot and mark the listed ones as background work."""
    pruned_count = 0
    marked_count = 0
    for subagent_id in _visible_entry_names(_plugin_dir(state_dir) / _SUBAGENTS_DIRNAME):
        subagent_path = _subagent_path(state_dir, subagent_id)
        lines = _read_lines(subagent_path)
        if not lines or lines[0] != parent_id:
            continue
        if subagent_id not in running_subagent_ids:
            _remove_file(subagent_path)
            pruned_count += 1
        elif _BACKGROUND_SUBAGENT_MARK not in lines[1:]:
            _write_text_atomically(subagent_path, f"{parent_id}\n{_BACKGROUND_SUBAGENT_MARK}\n")
            marked_count += 1
        else:
            pass
    return f"subagents-pruned={pruned_count} subagents-marked-background={marked_count}"


def _handle_session_start(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    if session_id is None:
        return "ignored:no-session-id"
    if _is_subagent_payload(payload):
        return "ignored:subagent-session"
    plugin_dir = _plugin_dir(state_dir)
    _write_text_atomically(plugin_dir / _SESSION_ID_FILENAME, session_id + "\n")
    is_new_root = not _is_known_root(state_dir, session_id)
    if is_new_root:
        _append_text(plugin_dir / _ROOTS_FILENAME, session_id + "\n")
    _remove_file(plugin_dir / _FORK_FROM_FILENAME)
    _touch(state_dir / SESSION_STARTED_FILENAME)
    # The tasks these stood for were processes the restart killed.
    lease_outcome = _write_work_leases(state_dir, [], now)
    return ("root:new" if is_new_root else "root:known") + lease_outcome


def _handle_user_prompt_submit(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    prompt_id = _payload_prompt_id(payload)
    if session_id is None or prompt_id is None:
        return "ignored:no-session-or-prompt-id"
    if _is_subagent_payload(payload):
        return "ignored:subagent-prompt"
    is_auto_wake = _is_auto_wake_prompt_id(prompt_id)
    tail = AUTO_WAKE_TAIL_MARKER if is_auto_wake else normalized_prompt_tail(_payload_field_text(payload, "prompt"))
    _write_text_atomically(_turn_path(state_dir, session_id), f"{prompt_id}\n{tail}\n")
    if prompt_id.startswith(SUBAGENT_WAKE_PROMPT_ID_PREFIX):
        _remove_file(_pending_wake_path(state_dir, session_id))
    _clear_session_permission_waiting(state_dir, session_id)
    recompute_outcome = _recompute_active_marker(state_dir, host_dir, now)
    # Appended last, so a probe that sees this line also sees the turn's marker.
    _append_text(_plugin_dir(state_dir) / SUBMISSIONS_LOG_FILENAME, f"{prompt_id} {tail}\n")
    return ("turn:start-auto-wake" if is_auto_wake else "turn:start") + recompute_outcome


def _handle_stop(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    reason = _payload_field_text(payload, "reason")
    if session_id is None or _is_subagent_payload(payload):
        return "ignored:not-a-root-session"
    if reason != _END_TURN_REASON:
        return f"ignored:reason={reason or '-'}"
    raw_background_tasks = payload.get("backgroundTasks")
    background_tasks = raw_background_tasks if isinstance(raw_background_tasks, list) else []
    _write_text_atomically(_plugin_dir(state_dir) / _BACKGROUND_TASKS_FILENAME, json.dumps(background_tasks) + "\n")
    reconcile_outcome = _reconcile_subagents_with_snapshot(
        state_dir, session_id, _running_subagent_ids(background_tasks)
    )
    # Ahead of the recompute, so no probe falls between the marker coming off and the leases
    # landing and reads the agent as waiting on its user.
    lease_outcome = _write_work_leases(state_dir, background_tasks, now)
    return f"stop {reconcile_outcome}" + lease_outcome + _recompute_active_marker(state_dir, host_dir, now)


def _end_stopped_subagent(state_dir: Path, host_dir: str, subagent_id: str, now: datetime) -> str:
    is_removed = _remove_file(_subagent_path(state_dir, subagent_id))
    _drop_lease(state_dir, subagent_id)
    _clear_session_permission_waiting(state_dir, subagent_id)
    outcome = "subagent:end" if is_removed else "ignored:unknown-subagent"
    return outcome + _recompute_active_marker(state_dir, host_dir, now)


def _handle_stop_failure(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    """End a failed subagent; a failed root turn ends at its later ``turn_completed{error}``."""
    session_id = _payload_session_id(payload)
    if session_id is None:
        return "ignored:no-session-id"
    if not _is_subagent_payload(payload):
        return "ignored:root-session"
    return _end_stopped_subagent(state_dir, host_dir, session_id, now)


def _handle_stop_cancelled(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    prompt_id = _payload_prompt_id(payload)
    if session_id is None:
        return "ignored:no-session-id"
    if _is_subagent_payload(payload):
        return _end_stopped_subagent(state_dir, host_dir, session_id, now)
    if prompt_id is None:
        return "ignored:no-prompt-id"
    return _complete_turn(state_dir, host_dir, session_id, prompt_id, now)


def _handle_subagent_start(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    parent_id = _payload_session_id(payload)
    subagent_id = _payload_id(payload, "subagentId", "subagent_id")
    if parent_id is None or subagent_id is None:
        return "ignored:no-session-or-subagent-id"
    subagent_path = _subagent_path(state_dir, subagent_id)
    if subagent_path.exists():
        return "subagent:known" + _recompute_active_marker(state_dir, host_dir, now)
    _write_text_atomically(subagent_path, parent_id + "\n")
    return "subagent:start" + _recompute_active_marker(state_dir, host_dir, now)


def _handle_session_end(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    if session_id is None:
        return "ignored:no-session-id"
    if _is_subagent_payload(payload):
        return _end_subagent_session(state_dir, host_dir, session_id, now)
    return _end_root_session(state_dir, host_dir, session_id, now)


def _apply_idle_prompt_backstop(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    """Clear the root session's turn whatever it is, since grok sends idle_prompt only for a settled session."""
    session_id = _payload_session_id(payload)
    if session_id is None or _is_subagent_payload(payload):
        return "ignored:not-a-root-session"
    is_turn_removed = _remove_file(_turn_path(state_dir, session_id))
    _remove_file(_pending_wake_path(state_dir, session_id))
    outcome = "backstop:cleared" if is_turn_removed else "backstop:no-turn"
    return outcome + _recompute_active_marker(state_dir, host_dir, now)


def _record_permission_waiting(state_dir: Path, session_id: Optional[str]) -> None:
    waiting_path = state_dir / PERMISSIONS_WAITING_FILENAME
    waiting_session_ids = _read_lines(waiting_path)
    if session_id is None or session_id in waiting_session_ids:
        _touch(waiting_path)
    else:
        _write_text_atomically(waiting_path, "".join(f"{waiting}\n" for waiting in [*waiting_session_ids, session_id]))


def _handle_notification(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    notification_type = _payload_text(payload, "notificationType", "notification_type")
    if notification_type in _PERMISSION_NOTIFICATION_TYPES:
        _record_permission_waiting(state_dir, _payload_session_id(payload))
        return "permissions:waiting"
    elif notification_type == _IDLE_PROMPT_NOTIFICATION_TYPE:
        return _apply_idle_prompt_backstop(state_dir, host_dir, payload, now)
    else:
        return f"ignored:notification={notification_type or '-'}"


def _handle_permission_resolved(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    return _clear_session_permission_waiting(state_dir, _payload_session_id(payload))


def _clear_session_permission_waiting(state_dir: Path, session_id: Optional[str]) -> str:
    """Remove the session's line from permissions_waiting; an id-less marker or event removes the whole marker."""
    waiting_path = state_dir / PERMISSIONS_WAITING_FILENAME
    waiting_session_ids = _read_lines(waiting_path)
    remaining_session_ids = [waiting for waiting in waiting_session_ids if waiting != session_id]
    if session_id is None or not waiting_session_ids or not remaining_session_ids:
        is_removed = _remove_file(waiting_path)
        return "permissions:cleared" if is_removed else "permissions:none"
    elif session_id not in waiting_session_ids:
        return "permissions:kept-other-session"
    else:
        _write_text_atomically(waiting_path, "".join(f"{waiting}\n" for waiting in remaining_session_ids))
        return "permissions:cleared-session"


def _handle_turn_end_command(state_dir: Path, host_dir: str, payload: Mapping[str, Any], now: datetime) -> str:
    session_id = _payload_session_id(payload)
    prompt_id = _payload_prompt_id(payload)
    if session_id is None or prompt_id is None:
        return "ignored:invalid-arguments"
    return _complete_turn(state_dir, host_dir, session_id, prompt_id, now)


_EventHandler = Callable[[Path, str, Mapping[str, Any], datetime], str]

HOOK_EVENT_HANDLERS: Final[Mapping[str, _EventHandler]] = {
    "session_start": _handle_session_start,
    "user_prompt_submit": _handle_user_prompt_submit,
    "stop": _handle_stop,
    "stop_failure": _handle_stop_failure,
    "stop_cancelled": _handle_stop_cancelled,
    "subagent_start": _handle_subagent_start,
    "session_end": _handle_session_end,
    "notification": _handle_notification,
    "post_tool_use": _handle_permission_resolved,
    "post_tool_use_failure": _handle_permission_resolved,
    "permission_denied": _handle_permission_resolved,
}


def _log_token(value: str) -> str:
    return _WHITESPACE_PATTERN.sub("_", value)[:_LOG_FIELD_MAX_LENGTH] or "-"


def _format_log_fields(event: str, payload: Mapping[str, Any], outcome: str) -> str:
    session = _log_token(_payload_text(payload, "sessionId", "session_id"))
    prompt = _log_token(_payload_text(payload, "promptId", "prompt_id"))
    return f"event={_log_token(event)} session={session} prompt={prompt} {_WHITESPACE_PATTERN.sub(' ', outcome)}"


def _run_under_state_lock(
    state_dir: Path,
    host_dir: str,
    handler: _EventHandler,
    payload: Mapping[str, Any],
    now: datetime,
    lock_timeout_seconds: float,
) -> str:
    with _held_state_lock(state_dir, lock_timeout_seconds):
        return handler(state_dir, host_dir, payload, now)


def _dispatch(
    state_dir: Path,
    host_dir: str,
    event: str,
    handler: Optional[_EventHandler],
    payload: Mapping[str, Any],
    now: datetime,
    lock_timeout_seconds: float,
) -> str:
    if handler is None:
        return _format_log_fields(event, payload, "ignored:unknown-event")
    try:
        outcome = _run_under_state_lock(state_dir, host_dir, handler, payload, now, lock_timeout_seconds)
    except StateLockTimeoutError:
        outcome = "skipped:state-lock-timeout"
    return _format_log_fields(event, payload, outcome)


def _run_invocation(
    state_dir_value: str, arguments: Sequence[str], stdin: BinaryIO, environ: Mapping[str, str], now: datetime
) -> Optional[str]:
    """Run one invocation and return its log line, or None when it must leave no trace."""
    host_dir = environ.get("MNGR_HOST_DIR", "")
    if len(arguments) == 0:
        raw_payload = stdin.read()
        if state_dir_value == "" or environ.get("GROK_AGENT") == "1":
            return None
        payload = _parse_payload(raw_payload)
        event = _hook_event_name(payload, environ)
        return _dispatch(
            Path(state_dir_value),
            host_dir,
            event,
            HOOK_EVENT_HANDLERS.get(event),
            payload,
            now,
            STATE_LOCK_TIMEOUT_SECONDS,
        )
    elif state_dir_value == "":
        return None
    elif len(arguments) == 3 and arguments[0] in (_TURN_COMPLETED_FLAG, TURN_DROPPED_FLAG):
        payload = {"sessionId": arguments[1], "promptId": arguments[2]}
        return _dispatch(
            Path(state_dir_value),
            host_dir,
            arguments[0],
            _handle_turn_end_command,
            payload,
            now,
            _COMMAND_STATE_LOCK_TIMEOUT_SECONDS,
        )
    else:
        return _format_log_fields("invocation", {}, "ignored:unknown-arguments " + " ".join(arguments))


def _describe_error(error: Exception) -> str:
    innermost_frame = error.__traceback__
    while innermost_frame is not None and innermost_frame.tb_next is not None:
        innermost_frame = innermost_frame.tb_next
    line = f" (line {innermost_frame.tb_lineno})" if innermost_frame is not None else ""
    return f"error:{type(error).__name__}: {error}{line}"


def _silence_descriptors(descriptors: Sequence[int]) -> None:
    devnull_descriptor = os.open(os.devnull, os.O_WRONLY)
    for descriptor in descriptors:
        os.dup2(devnull_descriptor, descriptor)
    os.close(devnull_descriptor)


def _append_hook_log_line(state_dir_value: str, now: datetime, log_fields: str) -> None:
    log_path = Path(state_dir_value).joinpath(*_HOOK_LOG_PATH_PARTS)
    try:
        _append_text(log_path, f"{_utc_timestamp(now)} {log_fields}\n")
    except OSError:
        # The log is the only diagnostic channel, so a failure to write it has nowhere to go.
        return


def main(argv: Sequence[str], stdin: BinaryIO, environ: Mapping[str, str], silenced_descriptors: Sequence[int]) -> int:
    """Run one invocation after pointing ``silenced_descriptors`` at ``/dev/null``; always returns 0."""
    now = datetime.now(timezone.utc)
    state_dir_value = environ.get("MNGR_AGENT_STATE_DIR", "")
    arguments = list(argv[1:])
    is_nested_hook = not arguments and environ.get("GROK_AGENT") == "1"
    # The process boundary: grok shows any non-zero exit or stderr text in the user's TUI.
    try:
        _silence_descriptors(silenced_descriptors)
        log_fields = _run_invocation(state_dir_value, arguments, stdin, environ, now)
    except Exception as error:
        log_fields = _format_log_fields(arguments[0] if arguments else "hook", {}, _describe_error(error))
    if log_fields is not None and state_dir_value != "" and not is_nested_hook:
        _append_hook_log_line(state_dir_value, now, log_fields)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin.buffer, os.environ, _STANDARD_STREAM_DESCRIPTORS))
