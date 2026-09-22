import fcntl
import json
import os
import queue
import select
import threading
from collections.abc import Callable
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

import psutil
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.api.discovery_aggregator import AggregatorDelta
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoverySchemaMismatchWarner
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.file_watch import DirectoryWatchGroup
from imbue.mngr.utils.file_watch import WATCHED_TAIL_FALLBACK_POLL_SECONDS
from imbue.mngr.utils.file_watch import start_event_forwarder
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.jsonl_warn import split_complete_lines

# === Constants ===

OBSERVE_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/agents")
AGENT_STATES_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/agent_states")
ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/activity")
OBSERVE_LOCK_FILENAME: Final[str] = "observe_lock"
FULL_STATE_INTERVAL_SECONDS: Final[float] = 300.0
# Timeout for the activity worker's queue get. Activity items are handled the
# moment they arrive (the get returns immediately), so this bounds only how
# often the worker re-checks that its child processes are still alive.
_ACTIVITY_QUEUE_POLL_SECONDS: Final[float] = 5.0
# Timeout for each psutil wait() call in a PID watcher's *fallback* loop (used
# only when the event-driven pidfd wait is unavailable, i.e. macOS or an old
# Linux kernel). Bounds how long such a watcher takes to notice a stop request
# (it cannot interrupt an in-flight wait); process death itself is detected
# event-driven by psutil, well before this elapses.
_WATCH_POLL_SECONDS: Final[float] = 3.0
# Cheap byte-level pre-filter so scanning a large event file does not run every
# line through ``json.loads``. Only lines containing the literal type token are
# parsed to confirm they really are full-state snapshots.
_FULL_STATE_MARKER: Final[bytes] = b'"AGENTS_FULL_STATE"'
# Default for how long ``ObserveEventFollower.stop`` waits for its thread to notice
# the stop flag. The thread only ever blocks in its wake wait, which ``stop``
# interrupts at once, or inside the consumer's sink, so this only has to cover
# a sink that is slow rather than stuck.
_FOLLOW_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0


# === Event Types ===


class ObserveEventType(UpperCaseStrEnum):
    """Type of agent observation event."""

    AGENT_STATE = auto()
    AGENTS_FULL_STATE = auto()
    AGENT_STATE_CHANGE = auto()
    AGENT_REMOVED = auto()


class AgentStateEvent(EventEnvelope):
    """An individual agent's current state, emitted when activity is detected on its host."""

    agent: AgentDetails = Field(description="AgentDetails for the agent")


class FullAgentStateEvent(EventEnvelope):
    """Full state snapshot of all known agents."""

    agents: tuple[AgentDetails, ...] = Field(description="AgentDetails for all known agents")


class AgentStateChangeEvent(EventEnvelope):
    """Emitted when an agent's lifecycle state or host state changes.

    Written to the agent_states event stream, separate from the main agents stream.
    """

    agent_id: AgentId = Field(description="ID of the agent whose state changed")
    agent_name: AgentName = Field(description="Name of the agent whose state changed")
    old_state: str | None = Field(description="Previous lifecycle state value, or None if first observation")
    new_state: str = Field(description="New lifecycle state value")
    old_host_state: str | None = Field(description="Previous host state value, or None if first observation")
    new_host_state: str | None = Field(description="New host state value")
    agent: AgentDetails = Field(description="Full AgentDetails at time of state change")


class AgentRemovedEvent(EventEnvelope):
    """Emitted on the agents stream when a previously-known agent is destroyed.

    The full observer already conveys create/update via AGENT_STATE and
    AGENTS_FULL_STATE; this closes the loop for removals so a consumer reading the
    agents stream (e.g. via ``--stream-events``) learns promptly that an agent is
    gone instead of inferring it from the next full snapshot.
    """

    agent_id: AgentId = Field(description="ID of the removed agent")
    agent_name: AgentName = Field(description="Name of the removed agent")
    host_id: HostId | None = Field(
        default=None,
        description=(
            "ID of the host the removed agent was on. Agent ids are unique per host, "
            "not globally, so consumers need the host to know which instance is gone. "
            "None only for event lines written before this field existed."
        ),
    )


# === Path Helpers ===


@pure
def get_default_events_base_dir(config: MngrConfig) -> Path:
    """Return the default base directory for observe events (the expanded default_host_dir)."""
    return config.default_host_dir.expanduser()


@pure
def get_observe_events_dir(events_base_dir: Path) -> Path:
    """Return the directory for agent observation event files."""
    return events_base_dir / "events" / "mngr" / "agents"


@pure
def get_observe_events_path(events_base_dir: Path) -> Path:
    """Return the path to the agent observation events JSONL file."""
    return get_observe_events_dir(events_base_dir) / "events.jsonl"


@pure
def get_agent_states_events_dir(events_base_dir: Path) -> Path:
    """Return the directory for agent state change event files."""
    return events_base_dir / "events" / "mngr" / "agent_states"


@pure
def get_agent_states_events_path(events_base_dir: Path) -> Path:
    """Return the path to the agent state change events JSONL file."""
    return get_agent_states_events_dir(events_base_dir) / "events.jsonl"


@pure
def get_observe_lock_path(events_base_dir: Path) -> Path:
    """Return the path to the observe lock file."""
    return events_base_dir / OBSERVE_LOCK_FILENAME


# === Event Construction ===


def _make_envelope_fields() -> tuple[IsoTimestamp, EventId]:
    """Generate the standard envelope fields for a new event."""
    timestamp = IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))
    event_id = EventId(generate_log_event_id())
    return timestamp, event_id


def make_agent_state_event(agent_details: AgentDetails) -> AgentStateEvent:
    """Build an event recording a single agent's state."""
    timestamp, event_id = _make_envelope_fields()
    return AgentStateEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENT_STATE),
        event_id=event_id,
        source=OBSERVE_EVENT_SOURCE,
        agent=agent_details,
    )


def make_full_agent_state_event(agents: Sequence[AgentDetails]) -> FullAgentStateEvent:
    """Build a full state snapshot event for all known agents."""
    timestamp, event_id = _make_envelope_fields()
    return FullAgentStateEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENTS_FULL_STATE),
        event_id=event_id,
        source=OBSERVE_EVENT_SOURCE,
        agents=tuple(agents),
    )


def make_agent_state_change_event(
    agent: AgentDetails,
    old_state: str | None,
    old_host_state: str | None,
) -> AgentStateChangeEvent:
    """Build an event recording a change in an agent's lifecycle or host state."""
    timestamp, event_id = _make_envelope_fields()
    return AgentStateChangeEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENT_STATE_CHANGE),
        event_id=event_id,
        source=AGENT_STATES_EVENT_SOURCE,
        agent_id=agent.id,
        agent_name=agent.name,
        old_state=old_state,
        new_state=agent.state.value,
        old_host_state=old_host_state,
        new_host_state=agent.host.state.value if agent.host.state is not None else None,
        agent=agent,
    )


def make_agent_removed_event(agent_id: AgentId, agent_name: AgentName, host_id: HostId) -> AgentRemovedEvent:
    """Build an event recording that a single agent instance was removed."""
    timestamp, event_id = _make_envelope_fields()
    return AgentRemovedEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENT_REMOVED),
        event_id=event_id,
        source=OBSERVE_EVENT_SOURCE,
        agent_id=agent_id,
        agent_name=agent_name,
        host_id=host_id,
    )


# === Event Parsing ===


def parse_observe_event_line(line: str) -> AgentStateEvent | FullAgentStateEvent | AgentRemovedEvent | None:
    """Parse one JSONL line from the agents stream into its observe event type.

    Handles exactly the event types written to the ``mngr/agents`` stream:
    AGENT_STATE, AGENTS_FULL_STATE, and AGENT_REMOVED. The AGENT_STATE_CHANGE
    events live on the separate ``mngr/agent_states`` stream and are not echoed
    by ``--stream-events``, so any other (or unknown) type returns None rather
    than raising -- this keeps a consumer robust to forward-compatible additions.

    Returns None for empty/whitespace-only lines and for unrecognized event
    types. Raises ``json.JSONDecodeError`` for malformed JSON and
    ``pydantic.ValidationError`` for a known type whose payload does not match
    the current schema (a real upstream problem that should surface).
    """
    stripped = line.strip()
    if not stripped:
        return None

    data = json.loads(stripped)
    event_type = data.get("type")
    if event_type == ObserveEventType.AGENT_STATE:
        return AgentStateEvent.model_validate(data)
    if event_type == ObserveEventType.AGENTS_FULL_STATE:
        return FullAgentStateEvent.model_validate(data)
    if event_type == ObserveEventType.AGENT_REMOVED:
        return AgentRemovedEvent.model_validate(data)
    return None


# === File I/O ===


def _append_event_to_file(events_path: Path, event: EventEnvelope) -> None:
    """Append a single event to a JSONL file.

    Creates parent directories if they do not exist. Uses a single write() call
    for safe concurrent appending under PIPE_BUF.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n"
    with open(events_path, "a") as f:
        f.write(line)


def append_observe_event(events_base_dir: Path, event: EventEnvelope) -> None:
    """Append a single observation event to the agents JSONL file."""
    _append_event_to_file(get_observe_events_path(events_base_dir), event)


def append_agent_state_change_event(events_base_dir: Path, event: AgentStateChangeEvent) -> None:
    """Append a state change event to the agent_states JSONL file."""
    _append_event_to_file(get_agent_states_events_path(events_base_dir), event)


# === Tracked State ===


class _TrackedState(FrozenModel):
    """Last known agent and host states for an agent, used for change detection."""

    agent_state: str
    host_state: str | None


def _details_instance_key(agent: AgentDetails) -> str:
    """Instance key (``<agent_id>@<host_id>``) for one probed AgentDetails.

    Agent ids are unique per host, not globally, so all per-agent observer
    tracking is keyed by the instance rather than the bare agent id.
    """
    return str(AgentInstanceKey.build(agent.id, agent.host.id))


# === History Loading ===


def _is_full_state_line(line: str) -> bool:
    """Whether a COMPLETE JSONL line is an ``AGENTS_FULL_STATE`` snapshot event.

    Callers must not pass a file's half-written tail (see
    :func:`find_last_full_state_offset`): a torn line is the one benign way this
    parse fails, so excluding it up front means a failure here is real corruption
    and is logged as such.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("Skipping an unparseable observe events line: {}", e)
        return False
    if not isinstance(data, dict):
        return False
    return data.get("type") == ObserveEventType.AGENTS_FULL_STATE


def find_last_full_state_offset(events_path: Path) -> int | None:
    """Byte offset of the last complete ``AGENTS_FULL_STATE`` line, or None if there is none.

    Where a reader that folds the whole agent set has to begin: a full snapshot is
    the only line such a view can be rebuilt from. Replaying a lone mid-stream
    ``AGENT_STATE`` first would collapse that view to the single agent it names.

    Scans the file once without retaining it, so a long-lived host's event history
    costs one sequential read and constant memory.

    Raises ``FileNotFoundError`` for an events file that does not exist -- the
    ordinary state of a host dir where no observer has ever run. That is kept
    distinct from None, which says the file is there and holds no snapshot yet: a
    caller may reasonably wait for the second and not the first.
    """
    return _scan_last_snapshot_and_boundary(events_path)[0]


def _scan_last_snapshot_and_boundary(events_path: Path) -> tuple[int | None, int]:
    """One pass over ``events_path``: the last complete snapshot's offset (or
    None), and the offset just past the last complete line.

    One pass rather than two lookups because the file has a live writer: with
    separate scans, a snapshot appended between "is there a snapshot?" (no) and
    "where does the tail start?" lands *before* the answered boundary and is
    silently skipped -- and a follower seeded that way drops every event until the
    writer's next snapshot. A single pass cannot see the file in two different
    states, so whatever it misses is at or past the boundary it answers.
    """
    last_snapshot_offset: int | None = None
    offset = 0
    end_of_last_complete_line = 0
    with open(events_path, "rb") as handle:
        for raw_line in handle:
            # Only a newline-terminated line is complete. Iterating a file yields
            # a concurrent writer's half-written tail as a final unterminated
            # "line", which a snapshot exceeding the atomic-append size hits
            # routinely (see ``_append_event_to_file``). Skipping it leaves
            # ``last_snapshot_offset`` on the previous snapshot -- where a fold
            # should start anyway, since replaying forward reaches this line once
            # the writer has finished it.
            #
            # The byte-level marker test keeps this whole-file scan from running
            # every line through ``json.loads``; only candidates are decoded.
            if raw_line.endswith(b"\n"):
                if _FULL_STATE_MARKER in raw_line and _is_full_state_line(raw_line.decode("utf-8", errors="replace")):
                    last_snapshot_offset = offset
                offset += len(raw_line)
                end_of_last_complete_line = offset
            else:
                offset += len(raw_line)
    return last_snapshot_offset, end_of_last_complete_line


def load_base_state_from_history(
    events_base_dir: Path,
) -> dict[str, _TrackedState]:
    """Load base agent and host state from the most recent full state event in history.

    Scans the observe events file for the latest AGENTS_FULL_STATE event and
    reconstructs the last known lifecycle and host states for each agent.

    Returns a dict mapping agent instance key (``<agent_id>@<host_id>``) ->
    _TrackedState. A history line missing host details (which current writers
    always include) falls back to the bare agent id for that line only.
    """
    events_path = get_observe_events_path(events_base_dir)
    if not events_path.exists():
        return {}

    latest_agents_data: tuple[dict, ...] | None = None
    warner = MalformedJsonLineWarner(source_description=f"observe events file '{events_path}'")
    with open(events_path) as f:
        for line in f:
            parsed = warner.parse(line)
            if parsed is None:
                continue
            data, _ = parsed
            if data.get("type") == ObserveEventType.AGENTS_FULL_STATE:
                latest_agents_data = tuple(data.get("agents", ()))

    if latest_agents_data is None:
        return {}

    last_state_by_instance: dict[str, _TrackedState] = {}
    for agent_dict in latest_agents_data:
        agent_id = agent_dict.get("id")
        if agent_id is not None:
            state = agent_dict.get("state")
            host_dict = agent_dict.get("host", {})
            host_state = host_dict.get("state") if isinstance(host_dict, dict) else None
            host_id = host_dict.get("id") if isinstance(host_dict, dict) else None
            instance_key = f"{agent_id}@{host_id}" if host_id is not None else str(agent_id)
            if state is not None:
                last_state_by_instance[instance_key] = _TrackedState(
                    agent_state=str(state),
                    host_state=str(host_state) if host_state is not None else None,
                )

    return last_state_by_instance


# === Locking ===


class ObserveLockError(MngrError):
    """Raised when another mngr observe instance is already writing to the same directory."""

    def __init__(self, events_base_dir: Path) -> None:
        super().__init__(
            f"Another 'mngr observe' instance is already writing to {events_base_dir}. "
            "Only one instance per output directory can run at a time."
        )


def acquire_observe_lock(events_base_dir: Path) -> int:
    """Acquire an exclusive file lock for the observe process.

    Returns the file descriptor (caller must keep it open to hold the lock).
    Raises ObserveLockError if another instance already holds the lock.
    """
    lock_path = get_observe_lock_path(events_base_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ObserveLockError(events_base_dir) from None
    return fd


def release_observe_lock(fd: int) -> None:
    """Release the observe file lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as e:
        logger.warning("Failed to unlock observe lock file: {}", e)
    try:
        os.close(fd)
    except OSError as e:
        logger.warning("Failed to close observe lock file descriptor: {}", e)


class ObserveLockProbeError(MngrError):
    """Raised when whether an observer holds the lock could not be determined.

    Distinct from a definitive "no observer": the answer is unknown, not negative.
    Callers must decide what to do about that themselves, because the right move
    differs -- a follower cannot attach and should say why, while a command that
    only wanted to avoid starting a second observer may reasonably press on.
    """

    def __init__(self, lock_path: Path, cause: OSError) -> None:
        super().__init__(f"Could not probe the observe lock at {lock_path}: {cause}")


def is_observe_writer_running(events_base_dir: Path) -> bool:
    """Whether some process currently holds the observe lock for this directory.

    This is the liveness signal for the event stream a follower reads: the observer
    holds the lock for its whole run, so "the lock is held" means "someone is
    writing the event file", and nothing else does.

    Implemented by trying to take the lock and immediately dropping it again;
    failing to take it *because someone else holds it* is the positive answer, and
    that is the only failure which is. The momentary hold in the negative case can
    in principle make an observer starting at that exact microsecond fail to take
    its own lock. Nothing cheaper avoids that -- a shared lock would block a
    starting writer's exclusive one just the same -- and the window is a single
    ``flock`` pair on a path nobody else contends for, so the exposure is accepted
    rather than eliminated. Unlike :func:`acquire_observe_lock` this never creates
    the lock file, and it asks only for read access: ``flock`` is advisory and
    independent of the open mode, so requesting write access would make a lock file
    owned by another user (an observer started as root during bootstrap, say) read
    as "no observer" while one is running.

    Raises :class:`ObserveLockProbeError` when the probe itself fails (a permission
    problem, a filesystem without ``flock``). Returning either answer there would
    state as fact something the evidence does not support, and every caller goes on
    to repeat that conclusion to a user.
    """
    lock_path = get_observe_lock_path(events_base_dir)
    try:
        fd = os.open(str(lock_path), os.O_RDONLY)
    except FileNotFoundError:
        # No observer has ever run here, so there is nothing to follow. This is the
        # one negative answer the probe can actually establish.
        return False
    except OSError as e:
        raise ObserveLockProbeError(lock_path, e) from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Someone else holds it: the only failure that means an observer is running.
        return True
    except OSError as e:
        raise ObserveLockProbeError(lock_path, e) from e
    finally:
        os.close(fd)
    return False


# === Following ===


class ObserveStreamUnavailableError(MngrError, ValueError):
    """Raised when there is no live observer whose event stream could be followed."""


class ObserveEventFollower(MutableModel):
    """Reads the agents event stream that another process's observer is writing.

    Exactly one process per host may *run* an observer (it needs the single-writer
    lock above), but the events it produces are appended to a plain JSONL file that
    any number of processes may *read*. This is the read side: a second consumer --
    a throwaway server booted alongside the real one, a monitoring tool -- follows
    the live stream instead of fighting for the lock and getting a dead observer.

    Feeds each JSONL line to ``on_line`` verbatim as the observer wrote it to the
    event file. That is the same event sequence ``mngr observe --stream-events``
    prints on stdout (only the JSON separators differ, since that path re-encodes),
    so a consumer folds identical events whether it owns the observer or follows one.

    Two invariants make the fold safe:

    - Folding starts at a full-state snapshot. On start we seek to the last one in
      the file and replay forward; if the file has none yet, every line is dropped
      until the observer emits one.
    - Only complete lines are forwarded. A snapshot of many agents exceeds the
      atomic-append size, so the tail can legitimately hold a half-written line; it
      is left in place and picked up on a later poll.

    Threading: ``start`` spawns one daemon thread that calls :meth:`poll_once`
    whenever the events directory changes (a watchdog directory watch, as the
    ``mngr event --follow`` tails use), and on a fallback interval otherwise --
    that interval only covers a filesystem event the watch missed, and bounds how
    late a lost observer is noticed. Tests drive :meth:`poll_once` directly
    instead, so the class needs no test-only seam.

    Two kinds of thing can stop events arriving, and they are handled differently:

    - An *outage* is environmental -- no observer holds the lock, or the lock could
      not be probed. The follow loop keeps running, :meth:`failure_detail` reports
      the outage for as long as it lasts, and the fold resumes on its own when an
      observer comes back (a new ``mngr observe`` appends a fresh full-state
      snapshot, which replaces the folded view). Nothing is lost by re-probing: a
      follower holds no lock, so it cannot take one away from the writer it wants.
    - A *failure* is internal -- the ``on_line`` sink raised, or the loop itself
      died. That is permanent and first-cause-wins, because retrying a consumer
      whose fold is broken just breaks it again.

    Whether an outage may also be the *starting* state is the consumer's call
    (``require_writer``). A consumer attaching to an observer it expects to be up
    wants ``start`` to refuse when none is, since tailing a dormant file is the
    failure this class exists to prevent. A consumer booted beside the observer by
    a supervisor, in no guaranteed order, cannot demand that: it starts anyway, and
    its follow thread reports the outage from its first tick onward and seeds from
    the file at the first tick that finds a writer -- never from a snapshot an
    unlocked file merely happens to hold.

    A follower is single-use: ``start`` may be called once, and not again after
    ``stop``. Construct a new one to follow the stream again.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=False)

    events_base_dir: Path = Field(frozen=True, description="Directory the observer writes its events under")
    on_line: Callable[[str], None] = Field(frozen=True, description="Sink for each complete event line")
    require_writer: bool = Field(
        default=True,
        frozen=True,
        description="Whether ``start`` refuses when no observer holds the lock, rather than waiting for one",
    )
    fallback_poll_seconds: float = Field(
        default=WATCHED_TAIL_FALLBACK_POLL_SECONDS,
        frozen=True,
        description="Longest the follow thread sleeps between tail reads when the directory watch stays quiet",
    )
    join_timeout_seconds: float = Field(
        default=_FOLLOW_JOIN_TIMEOUT_SECONDS,
        frozen=True,
        description="Seconds ``stop`` waits for the follow thread before reporting it still running",
    )

    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: ObservableThread | None = PrivateAttr(default=None)
    _watch_group: DirectoryWatchGroup | None = PrivateAttr(default=None)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until something internal makes the stream unfollowable for good.
    # "Never started" is not represented here -- that is the owner's fact, not the
    # follower's.
    _failure: str | None = PrivateAttr(default=None)
    # The environmental outage in effect right now, or None. Unlike ``_failure``
    # this is cleared as soon as a writer comes back, and it is refreshed rather
    # than kept first-cause-wins so it describes the *current* reason.
    _outage: str | None = PrivateAttr(default=None)
    _offset: int = PrivateAttr(default=0)
    _is_seeded: bool = PrivateAttr(default=False)
    _has_seen_snapshot: bool = PrivateAttr(default=False)
    _is_started: bool = PrivateAttr(default=False)
    # Whether a tick has ever found a writer. Only then can a later tick that
    # finds none say the observer *exited*; before that, nothing has run yet.
    _has_seen_writer: bool = PrivateAttr(default=False)

    def start(self) -> None:
        """Begin following the live observer's event stream.

        Raises :class:`ObserveStreamUnavailableError` when ``require_writer`` is
        set and no observer holds the lock, because there is then no stream to
        follow and silently tailing a dormant file is the exact failure this class
        exists to prevent. Without ``require_writer`` it starts anyway; the follow
        thread's first tick records the outage (``failure_detail`` then says no
        observer holds the lock), and it picks the stream up on the first tick that
        finds one.

        Also raises once this follower has been started or stopped, since neither
        way of starting it again does what the caller means: a second thread would
        share the first's read offset and forward every line twice, and a thread
        started with the stop flag already set would exit on its first loop check
        and -- because the flag is set -- record no failure, leaving
        ``is_stream_healthy`` reporting True forever. That second case includes a
        ``stop`` that ran before the follower ever started, e.g. from a ``finally``
        around a ``start`` that raised.
        """
        with self._lock:
            if self._is_started or self._stop_event.is_set():
                raise ObserveStreamUnavailableError(
                    "This agent-lifecycle follower has already been started or stopped; a follower is "
                    "single-use, so construct a new one to follow the stream again."
                )
            # Claimed under the lock, so two concurrent starts cannot both pass the
            # guard and spawn threads that share one read offset.
            self._is_started = True
        if self.require_writer:
            try:
                self._require_a_live_writer()
            except ObserveStreamUnavailableError:
                # Refusing here leaves the follower entirely unused, and the caller may
                # reasonably retry once an observer comes up, so give the claim back.
                with self._lock:
                    self._is_started = False
                raise
        watch_group = DirectoryWatchGroup()
        self._watch_group = watch_group
        thread = ObservableThread(
            target=self._follow_loop,
            args=(watch_group,),
            name="observe-follower",
            daemon=True,
            on_failure=self._on_follow_failure,
            # ``ObservableThread.join`` re-raises what the thread raised, and ``stop``
            # joins. The consumer's teardown is what calls ``stop``, and handing it its
            # own sink's exception there is not the contract: a dead stream is reported
            # through ``failure_detail()``.
            suppressed_exceptions=(BaseException,),
        )
        self._thread = thread
        thread.start()

    def _require_a_live_writer(self) -> None:
        """Raise :class:`ObserveStreamUnavailableError` unless an observer holds the lock."""
        try:
            is_writer_running = is_observe_writer_running(self.events_base_dir)
        except ObserveLockProbeError as e:
            # Unknown is not followable either, so the caller's answer is the same
            # error -- but it carries what actually went wrong rather than claiming
            # no observer is running.
            raise ObserveStreamUnavailableError(str(e)) from e
        if not is_writer_running:
            raise ObserveStreamUnavailableError(
                f"No 'mngr observe' process holds {get_observe_lock_path(self.events_base_dir)}, so there is "
                "no live agent-lifecycle event stream to follow."
            )

    def stop(self) -> None:
        """Stop the follow thread and wait briefly for it to exit. Idempotent.

        A thread that has not exited by the end of that wait is reported rather
        than assumed gone: it can only be stuck inside ``on_line``, so it is still
        feeding a consumer that has just been told the follower is stopped, and it
        is a daemon thread that will keep doing so until the process exits. There
        is no safe way to kill it from here, which is exactly why the caller has to
        hear about it.
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.join_timeout_seconds)
            if thread.is_alive():
                logger.warning(
                    "The agent-lifecycle follower thread did not exit within {} seconds and is still "
                    "running; its 'on_line' sink has most likely not returned, so it may keep "
                    "delivering events after this stop.",
                    self.join_timeout_seconds,
                )
            self._thread = None
        watch_group = self._watch_group
        if watch_group is not None:
            watch_group.stop()
            self._watch_group = None

    def is_stream_healthy(self) -> bool:
        """Whether events are reaching this follower right now.

        False while an outage is in effect and once an internal failure has been
        recorded. A follower that was never started, or was deliberately stopped,
        still reports True: neither is a fault of the stream, and which of those
        applies is the owner's own fact.
        """
        return self.failure_detail() is None

    def failure_detail(self) -> str | None:
        """Why the stream is not being followed right now, or None when it is.

        An internal failure wins over an outage: it is permanent, so it stays the
        answer even if a writer happens to be up.
        """
        with self._lock:
            return self._failure if self._failure is not None else self._outage

    def poll_once(self) -> None:
        """Advance by one tick: confirm the writer, then drain new lines.

        The writer check comes first so a stream that died between ticks is
        reported as dead rather than as "no new events", which is precisely the
        distinction a health gate needs. A tick that finds no writer records an
        outage and stops there; the next tick tries again, so an observer that
        restarts is picked back up without anyone re-attaching the follower.
        """
        try:
            is_writer_running = is_observe_writer_running(self.events_base_dir)
        except ObserveLockProbeError as e:
            # Says what actually happened. Reporting the observer as exited would
            # be a diagnosis we have no evidence for, and this string is what a
            # consumer puts in front of whoever has to fix it.
            self._record_outage(
                f"Could not tell whether the 'mngr observe' process writing "
                f"{get_observe_events_path(self.events_base_dir)} is still running, so agent lifecycle "
                f"events can no longer be relied on: {e}"
            )
            return
        if not is_writer_running:
            self._record_outage(self._describe_missing_writer())
            return
        self._has_seen_writer = True
        self._clear_outage()
        if not self._is_seeded:
            self._seed()
            return
        self._drain()

    def _describe_missing_writer(self) -> str:
        """The outage detail for a tick that found no writer, saying only what is known.

        "Exited" is a diagnosis this follower can make only once it has seen a
        writer; a follower started ahead of its observer has seen nothing exit.
        """
        if self._has_seen_writer:
            return (
                f"The 'mngr observe' process writing {get_observe_events_path(self.events_base_dir)} exited, "
                "so agent lifecycle events are no longer arriving."
            )
        return (
            f"No 'mngr observe' process holds the lock for {get_observe_events_path(self.events_base_dir)}, "
            "so agent lifecycle events are not arriving yet."
        )

    def _follow_loop(self, watch_group: DirectoryWatchGroup) -> None:
        """Poll until stopped, or until something internal makes the stream unfollowable.

        An outage does not end the loop: the observer it follows may come back, and
        re-probing a lock this follower never takes cannot get in that observer's way.
        The only other way out is an exception, which records a failure on the way
        past (see :meth:`_on_follow_failure`), so a consumer gating on
        :meth:`is_stream_healthy` cannot keep reporting itself live once this thread
        is gone.

        Pacing: the loop sleeps on a wake event that ``watch_group`` sets whenever
        the events directory changes, so a line is read the moment the writer
        appends it and an idle stream costs no wake-ups. The fallback wait covers
        what the watch cannot: a missed filesystem event, or a directory that did
        not exist yet when the watch was first attempted (an observer creates it on
        its first write), which is why the watch is re-attempted until it takes.
        ``stop`` wakes the loop immediately through a parked forwarder thread.
        """
        wake_event = threading.Event()
        start_event_forwarder(self._stop_event, wake_event, name="observe-follower-stop-forwarder")
        watched_dir = get_observe_events_dir(self.events_base_dir)
        is_watched = False
        while not self._stop_event.is_set():
            if not is_watched:
                is_watched = watch_group.watch(watched_dir, wake_event)
            self.poll_once()
            # A change landing between the clear below and the next read sets the
            # event again, so the next wait returns immediately -- nothing is lost.
            wake_event.wait(timeout=self.fallback_poll_seconds)
            wake_event.clear()

    def _on_follow_failure(self, e: BaseException) -> None:
        """Turn an exception that ended the follow thread into the consumer's diagnosis.

        Reached for anything that escapes :meth:`_follow_loop`, which includes
        whatever ``on_line`` raises -- this class cannot enumerate a consumer's fold.
        ``ObservableThread`` has already logged the traceback and re-raises after
        this returns, so nothing is swallowed; all that is left is to name the cause,
        since "something exited" is not actionable to whoever reads
        :meth:`failure_detail`. An exception that raced a deliberate ``stop`` is not a
        failure of the stream.
        """
        if self._stop_event.is_set():
            return
        self._record_failure(f"The agent-lifecycle follower stopped: {type(e).__name__}: {e}")

    def _seed(self) -> None:
        """Position at the newest full-state snapshot and replay from it to EOF."""
        events_path = get_observe_events_path(self.events_base_dir)
        self._is_seeded = True
        if not events_path.exists():
            self._offset = 0
            return
        # Both answers come from one scan (see ``_scan_last_snapshot_and_boundary``):
        # a snapshot the live writer appends after that scan sits at or past the
        # boundary, so the drain below -- or the next tick's -- picks it up rather
        # than losing it in the gap between two separate lookups.
        snapshot_offset, boundary = _scan_last_snapshot_and_boundary(events_path)
        if snapshot_offset is None:
            # No snapshot has ever been written. Skip the existing history (it
            # cannot be folded from) and wait at the tail; ``_has_seen_snapshot``
            # keeps dropping lines until the observer emits its next snapshot.
            #
            # Wait at the last line *boundary* rather than at EOF: seeking to a raw
            # size can land inside a line the writer is still appending, and the
            # remainder would then be read as though it were a whole line and logged
            # as corruption -- the one thing ``_is_full_state_line`` promises not to
            # cry wolf about.
            self._offset = boundary
            return
        self._offset = snapshot_offset
        self._drain()

    def _drain(self) -> None:
        """Forward every complete line appended since the last read.

        Replacement is detected by the file shrinking, which is all a size check can
        see: a replacement that happens to land on the same size reads as "no new
        events" and is deliberately out of scope (it would take tracking the inode).
        """
        events_path = get_observe_events_path(self.events_base_dir)
        if not events_path.exists():
            return
        size = events_path.stat().st_size
        if size < self._offset:
            # The file shrank, so it was truncated or replaced. Re-run the seed scan
            # over whatever is there now rather than reading garbage from a stale
            # offset -- and rather than replaying from byte 0, which would fold every
            # snapshot the replacement already holds and leave the view on an older
            # one than the file does.
            self._offset = 0
            self._is_seeded = False
            self._has_seen_snapshot = False
            self._seed()
            return
        if size == self._offset:
            return
        with open(events_path, "rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(size - self._offset)
        # A trailing line with no terminator is a write in progress -- routine here,
        # since a snapshot over the atomic-append size tears. ``split_complete_lines``
        # holds it back and reports the byte count actually consumed, so the offset
        # lands on it and it is re-read once the writer has finished it.
        lines, consumed_bytes = split_complete_lines(chunk.decode("utf-8", errors="replace"))
        self._offset += consumed_bytes
        for line in lines:
            self._forward(line)

    def _forward(self, line: str) -> None:
        """Hand one complete line to the sink, once folding has a snapshot to build on."""
        if not line.strip():
            return
        if not self._has_seen_snapshot:
            if not _is_full_state_line(line):
                return
            self._has_seen_snapshot = True
        self.on_line(line)

    def _record_failure(self, detail: str) -> None:
        """Mark the stream permanently dead (first cause wins) and say so loudly."""
        with self._lock:
            if self._failure is not None:
                return
            self._failure = detail
        logger.error("Agent lifecycle stream unavailable: {}", detail)

    def _record_outage(self, detail: str) -> None:
        """Note that no writer is reachable right now, refreshing the reason each tick.

        Logged only as the outage begins: it is re-recorded on every poll for as
        long as it lasts, and an observer that stays down would otherwise write one
        identical line per fallback poll for the whole outage.
        """
        with self._lock:
            is_new_outage = self._outage is None
            self._outage = detail
        if is_new_outage:
            logger.warning("Agent lifecycle stream unavailable: {}", detail)

    def _clear_outage(self) -> None:
        """Note that a writer is reachable again, if one was not."""
        with self._lock:
            was_out = self._outage is not None
            self._outage = None
        if was_out:
            logger.info(
                "Agent lifecycle stream recovered: an 'mngr observe' process is writing {} again",
                get_observe_events_path(self.events_base_dir),
            )


# === Observer ===


class _KnownHost(FrozenModel):
    """Tracks a discovered host."""

    host_id: HostId = Field(description="Unique identifier for the host")
    host_name: HostName = Field(description="Human-readable name of the host")


class _AgentWatcher(FrozenModel):
    """Bookkeeping for one local agent's PID-death watcher thread.

    ``pid`` is what the watcher is currently bound to, so a reconcile can tell
    whether the agent's main process changed. Holds a live thread and stop Event
    (hence arbitrary_types_allowed); it is never serialized.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    pid: int = Field(description="PID the watcher is bound to")
    stop_event: threading.Event = Field(description="Set to ask the watcher thread to stop")
    thread: threading.Thread = Field(description="The running watcher thread")
    stop_wake_write_fd: int = Field(
        description="Write end of the watcher's stop pipe; closing it wakes the thread's "
        "event-driven poll(2) wait (the read end sees POLLHUP)"
    )


def _signal_watcher_stop(watcher: _AgentWatcher) -> None:
    """Ask a watcher thread to stop and wake its (possibly poll(2)-blocked) wait.

    Closing the pipe's write end raises POLLHUP on the read end, which the
    pidfd wait treats as a stop request. Callers always pop the watcher from
    the registry (under the watchers lock) before signalling, so each watcher
    is signalled at most once and the fd is never double-closed.
    """
    watcher.stop_event.set()
    os.close(watcher.stop_wake_write_fd)


def _wait_for_pid_exit_via_pidfd(process: psutil.Process, stop_wake_read_fd: int) -> bool | None:
    """Event-driven wait for a process exit, with no wake-ups until something happens.

    Blocks in poll(2) on the process's pidfd (readable once the process exits)
    and the watcher's stop pipe (POLLHUP once the write end is closed by
    :func:`_signal_watcher_stop`). Returns True when the process exited, False
    on a stop request, and None when pidfd is unavailable on this platform
    (macOS, or a pre-5.3 Linux kernel) so the caller can fall back to the
    psutil polling wait. A process that is already gone counts as exited.
    """
    if not hasattr(os, "pidfd_open"):
        return None
    pid = process.pid
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError as e:
        logger.debug("pidfd_open unavailable for pid {} (falling back to psutil wait): {}", pid, e)
        return None
    try:
        # pidfd_open pinned whichever process owns the pid right now, while
        # ``process`` pinned (by create_time) whichever owned it at watcher
        # creation. is_running() compares the two: False means the watched
        # process already exited and the pid was recycled, so report the exit
        # instead of polling an unrelated process's pidfd forever.
        if not process.is_running():
            return True
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        poller.register(stop_wake_read_fd, select.POLLIN)
        # Blocks with no timeout; python retries EINTR internally. Exit wins a
        # tie so a death racing a stop request is still reported as an exit.
        ready_fds = {fd for fd, _event_mask in poller.poll()}
        return pidfd in ready_fds
    finally:
        os.close(pidfd)


def _make_unknown_agent_details(last_known: AgentDetails) -> AgentDetails:
    """Build a synthetic AgentDetails representing an UNKNOWN agent.

    Copies all fields from the last successfully-observed AgentDetails and
    overrides only the lifecycle states: ``state`` and ``host.state`` are both
    set to UNKNOWN, signalling "the provider that owns this agent could not be
    accessed during the most recent discovery attempt." Other fields (name,
    type, work_dir, etc.) retain their last-known values so the desktop client
    and ``mngr_notifications`` continue to identify the agent.
    """
    unknown_host = last_known.host.model_copy_update(
        to_update(last_known.host.field_ref().state, HostState.UNKNOWN),
    )
    return last_known.model_copy_update(
        to_update(last_known.field_ref().state, AgentLifecycleState.UNKNOWN),
        to_update(last_known.field_ref().host, unknown_host),
    )


@pure
def _is_provider_error_event(event: object) -> bool:
    """True if a discovery event indicates a provider failed this poll.

    Used to wake the observer's periodic snapshot loop so UNKNOWN state for the
    errored provider's agents propagates without waiting for the full interval.
    """
    if isinstance(event, ProviderDiscoverySnapshotEvent):
        return event.error is not None
    return isinstance(event, DiscoveryErrorEvent)


class AgentObserver(MutableModel):
    """Observes agent state changes across all hosts.

    Uses 'mngr observe --discovery-only' to track hosts and 'mngr event' to stream
    activity events from each online host. When activity is detected,
    fetches agent state and emits events to local JSONL files:

    - events/mngr/agents/events.jsonl: individual and full agent state snapshots
    - events/mngr/agent_states/events.jsonl: only when the lifecycle state field changes
    """

    mngr_ctx: MngrContext = Field(frozen=True)
    events_base_dir: Path = Field(frozen=True, description="Base directory for event output files and lock")
    mngr_binary: str = Field(default="mngr", frozen=True)
    # Optional sink invoked for every agents-stream event (AGENT_STATE /
    # AGENTS_FULL_STATE / AGENT_REMOVED) in addition to the file write, so a parent
    # process can consume state live (e.g. the CLI's --stream-events echoes each to
    # stdout). Injected rather than writing stdout here to keep this api-layer module
    # free of cli output concerns. The agent_states change stream is never sent here.
    agents_event_sink: Callable[[EventEnvelope], None] | None = Field(default=None, frozen=True)

    _concurrency_group: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="agent-observer"))
    # Folds the per-provider discovery stream into a consistent view (known hosts,
    # per-provider error state) that drives activity streams and UNKNOWN synthesis.
    _aggregator: DiscoveryStateAggregator = PrivateAttr(default_factory=DiscoveryStateAggregator)
    _known_hosts: dict[str, _KnownHost] = PrivateAttr(default_factory=dict)
    _discovery_stream_process: RunningProcess = PrivateAttr(default_factory=dict)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    # All per-agent tracking below is keyed by the agent *instance* key
    # (``<agent_id>@<host_id>``): agent ids are unique per host, not globally,
    # so the same id may exist on multiple hosts (e.g. mid-migration) and each
    # instance is tracked independently.
    _last_tracked_state_by_instance: dict[str, _TrackedState] = PrivateAttr(default_factory=dict)
    # PID-death watchers for local agents, keyed by agent instance. Each entry owns a
    # thread that blocks on psutil until the agent's main process exits, then
    # enqueues the agent's host for a re-probe so the death is emitted as state.
    _watchers: dict[str, _AgentWatcher] = PrivateAttr(default_factory=dict)
    _watchers_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Serializes agents_event_sink calls from the several threads that emit
    # agents-stream events (activity worker, snapshot loop, discovery-output
    # handler), so the sink's output never interleaves.
    _sink_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _activity_queue: queue.Queue[str] = PrivateAttr(default_factory=queue.Queue)
    # UNKNOWN-state tracking, keyed by agent instance. Populated only during
    # this process's lifetime (not from history) so that restart cannot
    # synthesize UNKNOWN for agents that may have been deliberately destroyed
    # while the observer was down.
    _last_known_details_by_instance: dict[str, AgentDetails] = PrivateAttr(default_factory=dict)
    # Most recently observed set of currently-errored providers (from discovery
    # snapshots and incremental DiscoveryErrorEvents). Agents whose provider is
    # in this set get an UNKNOWN AgentDetails synthesized on the next full state
    # snapshot if they did not reappear in the live listing.
    _currently_errored_providers: set[ProviderInstanceName] = PrivateAttr(default_factory=set)
    # Union of providers + errored providers currently known to the aggregator.
    # When a previously-tracked agent's provider is no longer in this set, the
    # observer treats it as implicit destroy (config-removal) and drops the
    # agent from tracking instead of marking it UNKNOWN.
    _known_provider_names: set[ProviderInstanceName] = PrivateAttr(default_factory=set)
    # Triggered to wake the periodic-snapshot loop early when a provider error is
    # observed, so UNKNOWN state propagates without waiting for the full poll interval.
    _snapshot_trigger: threading.Event = PrivateAttr(default_factory=threading.Event)
    # Deduplicates warnings for discovery lines that do not match this version's
    # schema (the child echoes lines other mngr versions wrote to the shared log).
    _discovery_schema_warner: DiscoverySchemaMismatchWarner = PrivateAttr(
        default_factory=lambda: DiscoverySchemaMismatchWarner(source_description="mngr observe discovery stream")
    )

    def run(self) -> None:
        """Run the observer. Blocks until stopped or interrupted."""
        with self._concurrency_group:
            # Load base state from event history so we can detect state changes since last run
            with log_span("Loading base state from history"):
                self._last_tracked_state_by_instance = load_base_state_from_history(self.events_base_dir)
                logger.debug(
                    "Loaded base state for {} agent(s) from history",
                    len(self._last_tracked_state_by_instance),
                )

            # Phase 1: initial full state snapshot
            with log_span("Performing initial full state snapshot"):
                self._do_full_state_snapshot()

            # Phase 2: start host discovery stream
            with log_span("Starting host discovery stream"):
                self._start_discovery_stream()

            # Phase 3: start the activity worker thread
            activity_worker = self._concurrency_group.start_new_thread(
                target=self._activity_worker,
                daemon=True,
                name="observe-activity-worker",
                on_failure=self._on_activity_failure,
            )

            # Phase 4: periodic full state snapshots + wait for stop
            try:
                while not self._stop_event.is_set():
                    # Wake early if a DiscoveryErrorEvent triggered a snapshot.
                    triggered = self._snapshot_trigger.wait(timeout=FULL_STATE_INTERVAL_SECONDS)
                    if triggered:
                        self._snapshot_trigger.clear()
                    if self._stop_event.is_set():
                        break
                    try:
                        with log_span("Performing periodic full state snapshot"):
                            self._do_full_state_snapshot()
                    except (MngrError, OSError) as e:
                        logger.warning("Periodic full state snapshot failed (continuing): {}", e)
            except KeyboardInterrupt:
                pass
            finally:
                self._stop_event.set()
                self._close_all_watchers()
                # Wake the activity worker out of its queue wait so it observes
                # the stop now instead of after the full queue poll timeout.
                # The empty sentinel is harmless: every post-get step checks
                # _stop_event first, and an unknown host id fetches nothing.
                self._activity_queue.put("")
                activity_worker.join(timeout=5.0)

    def _on_activity_failure(self, e: BaseException):
        logger.opt(exception=e).error("Activity worker thread failed")
        self._stop_event.set()
        self._snapshot_trigger.set()

    def stop(self) -> None:
        """Signal the observer to stop."""
        self._stop_event.set()
        # Unblock the periodic snapshot loop's wait on _snapshot_trigger.
        self._snapshot_trigger.set()

    def _start_discovery_stream(self) -> None:
        """Start the 'mngr observe --discovery-only' subprocess for host discovery."""
        self._discovery_stream_process = self._concurrency_group.run_process_in_background(
            command=[self.mngr_binary, "observe", "--discovery-only", "--quiet"],
            on_output=self._on_discovery_stream_output,
            is_checked_by_group=False,
            # This child streams a discovery snapshot per provider every poll interval
            # for as long as we run, so retaining its output would grow without bound.
            # Each line is consumed on arrival below, and stderr is logged there, so
            # nothing needs to read the output back afterwards.
            is_output_accumulated=False,
        )

    def _on_discovery_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from 'mngr observe --discovery-only'.

        Every discovery event is folded into the shared aggregator, which maintains the
        per-provider-correct view of hosts and provider error state. The returned delta
        drives starting/stopping per-host activity streams; provider error events wake
        the periodic snapshot loop so UNKNOWN state propagates quickly.
        """
        stripped = line.strip()
        if not is_stdout:
            # Logged rather than dropped: this process keeps no output history, so this
            # is the only place the child's stderr can still be seen.
            if stripped:
                logger.debug("mngr observe stderr: {}", stripped)
            return
        if not stripped:
            return

        try:
            event = self._discovery_schema_warner.parse(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse discovery line {!r}: {}", stripped[:200], e)
            return
        if event is None:
            return

        # Snapshot the agent map before applying so we can name agents that this
        # event removes (the delta carries only instance keys, and the aggregator
        # forgets the agent's data as part of applying the removal).
        agents_before = self._aggregator.get_agent_by_instance()
        with self._lock:
            known_host_ids_before = frozenset(self._known_hosts)
        delta = self._aggregator.apply_event(event)
        self._sync_known_state_from_aggregator()
        self._reconcile_activity_streams(known_host_ids_before)
        self._handle_agent_membership_delta(delta, agents_before)
        if _is_provider_error_event(event):
            self._snapshot_trigger.set()

    def _handle_agent_membership_delta(
        self, delta: AggregatorDelta, agents_before: dict[AgentInstanceKey, DiscoveredAgent]
    ) -> None:
        """React to agents appearing/disappearing in the discovery stream.

        The discovery stream is the low-latency membership signal. A newly
        discovered agent enqueues its host for a re-probe so its real lifecycle
        state (and pid) is emitted promptly, matching the near-instant create
        latency consumers had when they read the discovery stream directly. A
        removed agent instance emits an AGENT_REMOVED event on the agents stream
        and drops its per-agent tracking and PID watcher, so a consumer of
        --stream-events learns of the removal without waiting for the next full
        snapshot. Removal is instance-scoped: destroying an agent on one host
        never drops tracking for a same-id agent on another host.
        """
        if delta.added_agent_instances:
            agents_after = self._aggregator.get_agent_by_instance()
            for instance_key in delta.added_agent_instances:
                agent = agents_after.get(instance_key)
                if agent is not None:
                    self._activity_queue.put(str(agent.host_id))
        for instance_key in delta.removed_agent_instances:
            prior = agents_before.get(instance_key)
            agent_name = prior.agent_name if prior is not None else AgentName(str(instance_key.agent_id))
            self._emit_agent_removed(instance_key.agent_id, agent_name, instance_key.host_id)
            self._drop_agent_tracking(str(instance_key))

    def _drop_agent_tracking(self, instance_key_str: str) -> None:
        """Forget all per-agent state for a removed agent instance and close its PID watcher."""
        self._close_watcher(instance_key_str)
        with self._lock:
            self._last_tracked_state_by_instance.pop(instance_key_str, None)
            self._last_known_details_by_instance.pop(instance_key_str, None)

    def _sync_known_state_from_aggregator(self) -> None:
        """Refresh known hosts and provider error/known sets from the aggregator.

        A DESTROYED host is not a known host here: the aggregator retains it as a
        tombstone (and a provider may re-list it for its persistence window), but
        there is nothing left on it to stream activity from or fetch agent state
        for.
        """
        host_by_id = self._aggregator.get_host_by_id()
        new_known_hosts = {
            host_id_str: _KnownHost(host_id=host.host_id, host_name=host.host_name)
            for host_id_str, host in host_by_id.items()
            if host.host_state is not HostState.DESTROYED
        }
        errored_providers = set(self._aggregator.get_error_by_provider_name().keys())
        known_providers = {provider.provider_name for provider in self._aggregator.get_providers()} | errored_providers
        with self._lock:
            self._known_hosts = new_known_hosts
            self._currently_errored_providers = errored_providers
            self._known_provider_names = known_providers

    def _reconcile_activity_streams(self, known_host_ids_before: AbstractSet[str]) -> None:
        """Start activity streams for newly-known hosts and stop them for hosts no longer known."""
        with self._lock:
            known_hosts_after = dict(self._known_hosts)
        for host_id_str in known_host_ids_before - known_hosts_after.keys():
            self._stop_activity_stream(host_id_str)
        for host_id_str, host in known_hosts_after.items():
            if host_id_str not in known_host_ids_before:
                self._start_activity_stream(host_id_str, host.host_name)

    # FIXME: we'll need to be smarter about this when we have tons of hosts--add these options to the observe CLI and API:
    #  1. --local-watches-only to only observe the local host. If specified, don't bother starting an activity stream for anything besides the local host
    #  2. --no-watches to disable the activity streams entirely and just do periodic full snapshots (which will still emit change events, just with less granularity and more latency)
    def _start_activity_stream(self, host_id_str: str, host_name: HostName) -> None:
        """Start streaming activity events from a host."""
        with self._lock:
            if host_id_str in self._events_processes:
                return

        logger.debug("Starting activity stream for host {} ({})", host_name, host_id_str)
        try:
            process = self._concurrency_group.run_process_in_background(
                command=[
                    self.mngr_binary,
                    "event",
                    host_id_str,
                    str(ACTIVITY_EVENT_SOURCE),
                    "--follow",
                    "--quiet",
                ],
                on_output=lambda line, is_stdout: self._on_activity_event(line, is_stdout, host_id_str),
                is_checked_by_group=False,
                # A ``--follow`` stream lives as long as its host does, so retaining
                # every event it ever emits would grow without bound. Lines are
                # consumed on arrival by ``_on_activity_event``, which also logs stderr.
                is_output_accumulated=False,
            )
            with self._lock:
                self._events_processes[host_id_str] = process
        except (MngrError, OSError) as e:
            logger.debug("Failed to start activity stream for host {}: {}", host_name, e)

    def _stop_activity_stream(self, host_id_str: str) -> None:
        """Stop the activity event stream for a host."""
        with self._lock:
            process = self._events_processes.pop(host_id_str, None)
        if process is not None:
            logger.debug("Stopping activity stream for host {}", host_id_str)
            process.terminate()

    def _on_activity_event(self, line: str, is_stdout: bool, host_id_str: str) -> None:
        """Handle a line of activity event output from a host."""
        stripped = line.strip()
        if not is_stdout:
            # Logged rather than dropped: this process keeps no output history, so this
            # is the only place the child's stderr can still be seen.
            if stripped:
                logger.debug("mngr event stderr for host {}: {}", host_id_str, stripped)
            return
        if not stripped:
            return
        logger.trace("Activity event from host {}: {}", host_id_str, stripped[:200])
        self._activity_queue.put(host_id_str)

    def _activity_worker(self) -> None:
        """Worker thread that processes activity events and fetches agent state."""
        while not self._stop_event.is_set():
            # make sure that none of our processes crashed
            with self._lock:
                self._discovery_stream_process.check()
                for _host_id_str, event_process in self._events_processes.items():
                    event_process.check()

            # see if there are any activity events
            try:
                host_id_str = self._activity_queue.get(timeout=_ACTIVITY_QUEUE_POLL_SECONDS)
            except queue.Empty:
                continue

            # Drain additional entries to debounce rapid activity
            hosts_to_fetch: set[str] = {host_id_str}
            for _ in range(self._activity_queue.qsize()):
                try:
                    hosts_to_fetch.add(self._activity_queue.get_nowait())
                except queue.Empty:
                    break
            for hid in hosts_to_fetch:
                if self._stop_event.is_set():
                    break
                try:
                    self._fetch_and_emit_agent_state_for_host(hid)
                except (MngrError, OSError) as e:
                    logger.warning("Failed to fetch agent state for host {}: {}", hid, e)

    def _fetch_and_emit_agent_state_for_host(self, host_id_str: str) -> None:
        """Fetch current agent state for a host and emit events for all agents."""
        with self._lock:
            host = self._known_hosts.get(host_id_str)
        if host is None:
            return

        with log_span("Fetching agent state for host {}", host.host_name):
            result = list_agents(
                mngr_ctx=self.mngr_ctx,
                is_streaming=False,
                include_filters=(f'host.id == "{host.host_id}"',),
                error_behavior=ErrorBehavior.CONTINUE,
            )

        for agent in result.agents:
            self._emit_agent_state(agent)

    def _do_full_state_snapshot(self) -> None:
        """Perform a full listing, emit a full state event, and check for state changes.

        Agents that were previously observed within this process's lifetime but
        did not appear in the live listing are synthesized as UNKNOWN entries
        if their provider is currently errored (sticky until they reappear or
        the user explicitly destroys them). Agents whose provider has been
        removed from the configured set entirely are dropped from tracking.
        """
        result = list_agents(
            mngr_ctx=self.mngr_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        if result.errors:
            for error in result.errors:
                logger.warning("Error during full state snapshot: {} - {}", error.exception_type, error.message)

        self._process_snapshot_agents(result.agents)

    def _process_snapshot_agents(self, agents: Sequence[AgentDetails]) -> None:
        """Process agents from a full snapshot: detect state changes, emit events, update tracking.

        Synthesizes UNKNOWN entries for previously-observed agents that did not
        appear in `agents` if their provider is currently in the errored set
        (or the polling loop has crashed). Drops previously-observed agents
        whose provider is no longer configured at all.
        """
        live_instance_keys = {_details_instance_key(agent) for agent in agents}

        # Build UNKNOWN synthetic entries and per-instance drops in a single locked
        # region so the provider-error state we use to classify each missing
        # agent stays consistent with the dict mutations we do below.
        unknown_agents: list[AgentDetails] = []
        instance_keys_to_drop: list[str] = []
        with self._lock:
            # First, record everything we just observed.
            for agent in agents:
                self._last_known_details_by_instance[_details_instance_key(agent)] = agent

            errored_providers = self._currently_errored_providers
            known_providers = self._known_provider_names

            for instance_key_str, last_details in self._last_known_details_by_instance.items():
                if instance_key_str in live_instance_keys:
                    continue
                provider = last_details.host.provider_name
                # Config removal trumps everything: provider no longer in any current set.
                # Skip this rule if we don't yet have a known-provider list (first snapshot).
                if known_providers and provider not in known_providers:
                    instance_keys_to_drop.append(instance_key_str)
                    continue
                # Provider currently errored -- its agents' state is unknown, synthesize UNKNOWN.
                if provider in errored_providers:
                    unknown_agents.append(_make_unknown_agent_details(last_details))
                    continue
                # Provider is healthy and the agent disappeared from the listing without
                # an explicit destroy. Treat as implicit destroy (drop).
                instance_keys_to_drop.append(instance_key_str)

            for instance_key_str in instance_keys_to_drop:
                self._last_known_details_by_instance.pop(instance_key_str, None)
                # Stop tracking state-change history for dropped agents too; otherwise
                # an agent re-created with the same id later would appear to "change
                # state" relative to the stale tracked record.
                self._last_tracked_state_by_instance.pop(instance_key_str, None)

            # Update last-known details with the synthesized UNKNOWN versions so
            # subsequent polls don't re-synthesize from the pre-UNKNOWN details.
            for unknown_agent in unknown_agents:
                self._last_known_details_by_instance[_details_instance_key(unknown_agent)] = unknown_agent

        emitted_agents = tuple(agents) + tuple(unknown_agents)

        # Detect state changes against `_last_tracked_state_by_instance`
        state_changes: list[tuple[AgentDetails, str | None, str | None]] = []
        with self._lock:
            for agent in emitted_agents:
                instance_key_str = _details_instance_key(agent)
                new_agent_state = agent.state.value
                new_host_state = agent.host.state.value if agent.host.state is not None else None
                tracked = self._last_tracked_state_by_instance.get(instance_key_str)
                old_agent_state = tracked.agent_state if tracked else None
                old_host_state = tracked.host_state if tracked else None
                if old_agent_state != new_agent_state or old_host_state != new_host_state:
                    state_changes.append((agent, old_agent_state, old_host_state))
                    self._last_tracked_state_by_instance[instance_key_str] = _TrackedState(
                        agent_state=new_agent_state,
                        host_state=new_host_state,
                    )

        # Emit the full state event (includes all agents regardless of change)
        event = make_full_agent_state_event(emitted_agents)
        self._emit_observe_event(event)
        logger.debug(
            "Emitted full agent state event with {} agent(s) ({} live, {} unknown)",
            len(emitted_agents),
            len(agents),
            len(unknown_agents),
        )

        # Emit state change events to the agent_states stream
        for agent, old_agent_state, old_host_state in state_changes:
            self._emit_state_change(agent, old_agent_state, old_host_state)

        # Reconcile PID watchers from the live listing (open/replace/close by
        # pid), and close watchers for agents dropped from tracking. The
        # synthesized UNKNOWN agents are intentionally left untouched: their
        # provider is unreachable, so the last-known watcher (if any) stays as-is.
        for agent in agents:
            self._reconcile_watcher_for_agent(agent)
        for instance_key_str in instance_keys_to_drop:
            self._close_watcher(instance_key_str)

    # === PID Watchers (local agents only) ===

    def _reconcile_watcher_for_agent(self, agent: AgentDetails) -> None:
        """Open, replace, or close the PID watcher for one agent from its probed details.

        Only local agents are watched: a remote agent's ``pid`` is a PID in the
        remote host's namespace, so watching it here would watch an unrelated
        same-numbered local process. Locality is keyed on the ``local`` provider,
        the one and only user of the local connector. A remote agent, or a local
        agent with no ``pid`` (no longer running), closes any existing watcher.
        """
        instance_key_str = _details_instance_key(agent)
        if agent.host.provider_name != LOCAL_PROVIDER_NAME or agent.pid is None:
            self._close_watcher(instance_key_str)
            return
        self._open_or_replace_watcher(instance_key_str, str(agent.host.id), agent.pid)

    def _open_or_replace_watcher(self, instance_key_str: str, host_id_str: str, pid: int) -> None:
        """Ensure a watcher thread is running for ``pid``, replacing one on a stale PID.

        Held under ``_watchers_lock`` for its whole duration so two reconcile paths
        (the activity worker and the snapshot loop) cannot each start a thread for
        the same agent and leak one. The stale-watcher stop/join is inlined rather
        than delegated to ``_close_watcher`` to avoid re-acquiring the non-reentrant
        lock; joining here is deadlock-free because ``_watch_pid`` never takes it.
        """
        with self._watchers_lock:
            existing = self._watchers.get(instance_key_str)
            if existing is not None and existing.pid == pid:
                return
            # New agent or the main process changed (PID differs): stop the stale
            # watcher first, then start a fresh one bound to the current PID.
            if existing is not None:
                self._watchers.pop(instance_key_str, None)
                _signal_watcher_stop(existing)
                existing.thread.join(timeout=5.0)
            try:
                process = psutil.Process(pid)
            except psutil.NoSuchProcess:
                # The process is already gone. Enqueue a re-probe so the next listing
                # emits the stopped/done state rather than silently missing the death.
                self._activity_queue.put(host_id_str)
                return
            stop_event = threading.Event()
            # The stop pipe wakes the thread's event-driven poll(2) wait; the
            # thread owns (and closes) the read end, the _AgentWatcher entry owns
            # the write end (closed by _signal_watcher_stop).
            stop_wake_read_fd, stop_wake_write_fd = os.pipe()
            # is_checked=False so a single watcher's failure is isolated (logged via
            # on_failure) instead of being re-raised at the next strand start / group
            # exit, which would poison the whole ConcurrencyGroup and stop all
            # observation -- see _on_watcher_failure for the intended isolation.
            is_thread_started = False
            try:
                thread = self._concurrency_group.start_new_thread(
                    target=lambda: self._watch_pid(
                        instance_key_str, host_id_str, process, pid, stop_event, stop_wake_read_fd
                    ),
                    daemon=True,
                    name=f"observe-pid-watch-{instance_key_str[:14]}",
                    on_failure=self._on_watcher_failure,
                    is_checked=False,
                )
                is_thread_started = True
            finally:
                if not is_thread_started:
                    os.close(stop_wake_read_fd)
                    os.close(stop_wake_write_fd)
            self._watchers[instance_key_str] = _AgentWatcher(
                pid=pid, stop_event=stop_event, thread=thread, stop_wake_write_fd=stop_wake_write_fd
            )

    def _watch_pid(
        self,
        instance_key_str: str,
        host_id_str: str,
        process: psutil.Process,
        pid: int,
        stop_event: threading.Event,
        stop_wake_read_fd: int,
    ) -> None:
        """Block until the watched process exits (or a stop is requested), then signal activity.

        On Linux the wait is fully event-driven: a poll(2) over the process's
        pidfd and the watcher's stop pipe blocks with no timer at all. When
        pidfd is unavailable (macOS, old kernels) it falls back to psutil's
        wait -- itself event-driven for process death -- polled with a short
        timeout only to notice stop requests.
        """
        try:
            pidfd_wait_result = _wait_for_pid_exit_via_pidfd(process, stop_wake_read_fd)
            if pidfd_wait_result is None:
                is_exited = self._wait_for_pid_exit_via_psutil(instance_key_str, process, pid, stop_event)
            else:
                is_exited = pidfd_wait_result
        finally:
            os.close(stop_wake_read_fd)
        if not is_exited:
            return
        # A stop request that raced the exit means this watcher was replaced or
        # the observer is shutting down -- the re-probe is no longer ours to ask for.
        if stop_event.is_set() or self._stop_event.is_set():
            return
        logger.debug(
            "Local agent {} main process (pid {}) exited; enqueueing host {} for re-probe",
            instance_key_str,
            pid,
            host_id_str,
        )
        self._activity_queue.put(host_id_str)

    def _wait_for_pid_exit_via_psutil(
        self,
        instance_key_str: str,
        process: psutil.Process,
        pid: int,
        stop_event: threading.Event,
    ) -> bool:
        """Fallback wait for platforms without pidfd. True when the process exited, False on a stop request."""
        while not (stop_event.is_set() or self._stop_event.is_set()):
            try:
                process.wait(timeout=_WATCH_POLL_SECONDS)
            except psutil.TimeoutExpired:
                continue
            except (psutil.Error, OSError) as e:
                # psutil.Process.wait() can surface a bare OSError (not a psutil.Error)
                # when its underlying os.pidfd_open/kqueue/poll fails; treat any such
                # failure the same as an exit and re-probe rather than crash the watcher.
                logger.debug("PID watch for agent {} (pid {}) errored, treating as exit: {}", instance_key_str, pid, e)
            return True
        return False

    def _close_watcher(self, instance_key_str: str) -> None:
        """Stop and join the watcher for an agent, if any. Idempotent.

        Held under ``_watchers_lock`` through the join (deadlock-free because the
        watcher thread never takes that lock) so it cannot race a concurrent
        reconcile into leaving two entries for the same agent.
        """
        with self._watchers_lock:
            watcher = self._watchers.pop(instance_key_str, None)
            if watcher is None:
                return
            _signal_watcher_stop(watcher)
            watcher.thread.join(timeout=5.0)

    def _close_all_watchers(self) -> None:
        """Tear down every PID watcher (observer shutdown)."""
        with self._watchers_lock:
            instance_key_strs = list(self._watchers.keys())
        for instance_key_str in instance_key_strs:
            self._close_watcher(instance_key_str)

    def _on_watcher_failure(self, e: BaseException) -> None:
        """Log an unexpected watcher-thread failure without tearing down the observer.

        One local agent's watch dying should not stop observing every other agent;
        the periodic snapshot still catches that agent's death, just less promptly.
        """
        logger.opt(exception=e).warning("PID watcher thread failed")

    def _emit_observe_event(self, event: EventEnvelope) -> None:
        """Append an agents-stream event to its file and forward it to the sink when set.

        The file write is the canonical event bus (history replay, multi-consumer
        tailing); the sink is the additive opt-in for a parent process that consumes
        events live. The sink is called under a lock so events from the observer's
        several threads never interleave in the sink's output.
        """
        append_observe_event(self.events_base_dir, event)
        if self.agents_event_sink is not None:
            with self._sink_lock:
                self.agents_event_sink(event)

    def _emit_agent_removed(self, agent_id: AgentId, agent_name: AgentName, host_id: HostId) -> None:
        """Emit an AGENT_REMOVED event to the agents stream for a destroyed agent instance."""
        event = make_agent_removed_event(agent_id, agent_name, host_id)
        self._emit_observe_event(event)
        logger.debug("Emitted agent removed event for {} ({} on host {})", agent_name, agent_id, host_id)

    def _emit_agent_state(self, agent: AgentDetails) -> None:
        """Emit a single agent state event, check for state/host state change, and update tracking."""
        event = make_agent_state_event(agent)
        self._emit_observe_event(event)
        logger.debug("Emitted agent state event for {} (state={})", agent.name, agent.state.value)

        instance_key_str = _details_instance_key(agent)
        new_agent_state = agent.state.value
        new_host_state = agent.host.state.value if agent.host.state is not None else None

        with self._lock:
            tracked = self._last_tracked_state_by_instance.get(instance_key_str)
            old_agent_state = tracked.agent_state if tracked else None
            old_host_state = tracked.host_state if tracked else None
            self._last_tracked_state_by_instance[instance_key_str] = _TrackedState(
                agent_state=new_agent_state,
                host_state=new_host_state,
            )

        if old_agent_state != new_agent_state or old_host_state != new_host_state:
            self._emit_state_change(agent, old_agent_state, old_host_state)

        # Keep this agent's PID watcher in sync with what we just observed.
        self._reconcile_watcher_for_agent(agent)

    def _emit_state_change(self, agent: AgentDetails, old_state: str | None, old_host_state: str | None) -> None:
        """Emit a state change event to the agent_states stream."""
        state_change_event = make_agent_state_change_event(agent, old_state, old_host_state)
        append_agent_state_change_event(self.events_base_dir, state_change_event)
        logger.debug(
            "Emitted agent state change for {} ({} -> {})",
            agent.name,
            old_state,
            agent.state.value,
        )
