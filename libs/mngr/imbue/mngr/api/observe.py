import fcntl
import json
import os
import queue
import threading
from collections.abc import Callable
from collections.abc import Sequence
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
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner

# === Constants ===

OBSERVE_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/agents")
AGENT_STATES_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/agent_states")
ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("mngr/activity")
OBSERVE_LOCK_FILENAME: Final[str] = "observe_lock"
FULL_STATE_INTERVAL_SECONDS: Final[float] = 300.0
_ACTIVITY_DEBOUNCE_SECONDS: Final[float] = 2.0
# Timeout for each psutil wait() call in a PID watcher's loop. Bounds how long a
# watcher takes to notice a stop request (it cannot interrupt an in-flight wait),
# so it must stay small; process death itself is detected event-driven, well
# before this elapses.
_WATCH_POLL_SECONDS: Final[float] = 1.0


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


def make_agent_removed_event(agent_id: AgentId, agent_name: AgentName) -> AgentRemovedEvent:
    """Build an event recording that a single agent was removed."""
    timestamp, event_id = _make_envelope_fields()
    return AgentRemovedEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENT_REMOVED),
        event_id=event_id,
        source=OBSERVE_EVENT_SOURCE,
        agent_id=agent_id,
        agent_name=agent_name,
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


# === History Loading ===


def load_base_state_from_history(
    events_base_dir: Path,
) -> dict[str, _TrackedState]:
    """Load base agent and host state from the most recent full state event in history.

    Scans the observe events file for the latest AGENTS_FULL_STATE event and
    reconstructs the last known lifecycle and host states for each agent.

    Returns a dict mapping agent ID -> _TrackedState.
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

    last_state_by_id: dict[str, _TrackedState] = {}
    for agent_dict in latest_agents_data:
        agent_id = agent_dict.get("id")
        if agent_id is not None:
            state = agent_dict.get("state")
            host_dict = agent_dict.get("host", {})
            host_state = host_dict.get("state") if isinstance(host_dict, dict) else None
            if state is not None:
                last_state_by_id[str(agent_id)] = _TrackedState(
                    agent_state=str(state),
                    host_state=str(host_state) if host_state is not None else None,
                )

    return last_state_by_id


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
    _last_tracked_state_by_id: dict[str, _TrackedState] = PrivateAttr(default_factory=dict)
    # PID-death watchers for local agents, keyed by agent id. Each entry owns a
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
    # UNKNOWN-state tracking. Populated only during this process's lifetime
    # (not from history) so that restart cannot synthesize UNKNOWN for agents
    # that may have been deliberately destroyed while the observer was down.
    _last_known_details_by_id: dict[str, AgentDetails] = PrivateAttr(default_factory=dict)
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

    def run(self) -> None:
        """Run the observer. Blocks until stopped or interrupted."""
        with self._concurrency_group:
            # Load base state from event history so we can detect state changes since last run
            with log_span("Loading base state from history"):
                self._last_tracked_state_by_id = load_base_state_from_history(self.events_base_dir)
                logger.debug(
                    "Loaded base state for {} agent(s) from history",
                    len(self._last_tracked_state_by_id),
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
        )

    def _on_discovery_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from 'mngr observe --discovery-only'.

        Every discovery event is folded into the shared aggregator, which maintains the
        per-provider-correct view of hosts and provider error state. The returned delta
        drives starting/stopping per-host activity streams; provider error events wake
        the periodic snapshot loop so UNKNOWN state propagates quickly.
        """
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return

        event = parse_discovery_event_line(stripped)
        if event is None:
            return

        # Snapshot the agent map before applying so we can name agents that this
        # event removes (the delta carries only ids, and the aggregator forgets
        # the agent's data as part of applying the removal).
        agents_before = self._aggregator.get_agent_by_id()
        delta = self._aggregator.apply_event(event)
        self._sync_known_state_from_aggregator()
        self._reconcile_activity_streams(delta)
        self._handle_agent_membership_delta(delta, agents_before)
        if _is_provider_error_event(event):
            self._snapshot_trigger.set()

    def _handle_agent_membership_delta(
        self, delta: AggregatorDelta, agents_before: dict[str, DiscoveredAgent]
    ) -> None:
        """React to agents appearing/disappearing in the discovery stream.

        The discovery stream is the low-latency membership signal. A newly
        discovered agent enqueues its host for a re-probe so its real lifecycle
        state (and main_pid) is emitted promptly, matching the near-instant create
        latency consumers had when they read the discovery stream directly. A
        removed agent emits an AGENT_REMOVED event on the agents stream and drops
        its per-agent tracking and PID watcher, so a consumer of --stream-events
        learns of the removal without waiting for the next full snapshot.
        """
        if delta.added_agent_ids:
            agents_after = self._aggregator.get_agent_by_id()
            for agent_id_str in delta.added_agent_ids:
                agent = agents_after.get(agent_id_str)
                if agent is not None:
                    self._activity_queue.put(str(agent.host_id))
        for agent_id_str in delta.removed_agent_ids:
            prior = agents_before.get(agent_id_str)
            agent_name = prior.agent_name if prior is not None else AgentName(agent_id_str)
            self._emit_agent_removed(AgentId(agent_id_str), agent_name)
            self._drop_agent_tracking(agent_id_str)

    def _drop_agent_tracking(self, agent_id_str: str) -> None:
        """Forget all per-agent state for a removed agent and close its PID watcher."""
        self._close_watcher(agent_id_str)
        with self._lock:
            self._last_tracked_state_by_id.pop(agent_id_str, None)
            self._last_known_details_by_id.pop(agent_id_str, None)

    def _sync_known_state_from_aggregator(self) -> None:
        """Refresh known hosts and provider error/known sets from the aggregator."""
        host_by_id = self._aggregator.get_host_by_id()
        new_known_hosts = {
            host_id_str: _KnownHost(host_id=host.host_id, host_name=host.host_name)
            for host_id_str, host in host_by_id.items()
        }
        errored_providers = set(self._aggregator.get_error_by_provider_name().keys())
        known_providers = {provider.provider_name for provider in self._aggregator.get_providers()} | errored_providers
        with self._lock:
            self._known_hosts = new_known_hosts
            self._currently_errored_providers = errored_providers
            self._known_provider_names = known_providers

    def _reconcile_activity_streams(self, delta: AggregatorDelta) -> None:
        """Start activity streams for newly-known hosts and stop them for removed hosts."""
        for host_id_str in delta.removed_host_ids:
            self._stop_activity_stream(host_id_str)
        for host_id_str in delta.added_host_ids:
            with self._lock:
                host = self._known_hosts.get(host_id_str)
            if host is not None:
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
        if not is_stdout:
            return
        stripped = line.strip()
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
                host_id_str = self._activity_queue.get(timeout=_ACTIVITY_DEBOUNCE_SECONDS)
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
        live_agent_ids = {str(agent.id) for agent in agents}

        # Build UNKNOWN synthetic entries and per-id drops in a single locked
        # region so the provider-error state we use to classify each missing
        # agent stays consistent with the dict mutations we do below.
        unknown_agents: list[AgentDetails] = []
        ids_to_drop: list[str] = []
        with self._lock:
            # First, record everything we just observed.
            for agent in agents:
                self._last_known_details_by_id[str(agent.id)] = agent

            errored_providers = self._currently_errored_providers
            known_providers = self._known_provider_names

            for agent_id_str, last_details in self._last_known_details_by_id.items():
                if agent_id_str in live_agent_ids:
                    continue
                provider = last_details.host.provider_name
                # Config removal trumps everything: provider no longer in any current set.
                # Skip this rule if we don't yet have a known-provider list (first snapshot).
                if known_providers and provider not in known_providers:
                    ids_to_drop.append(agent_id_str)
                    continue
                # Provider currently errored -- its agents' state is unknown, synthesize UNKNOWN.
                if provider in errored_providers:
                    unknown_agents.append(_make_unknown_agent_details(last_details))
                    continue
                # Provider is healthy and the agent disappeared from the listing without
                # an explicit destroy. Treat as implicit destroy (drop).
                ids_to_drop.append(agent_id_str)

            for agent_id_str in ids_to_drop:
                self._last_known_details_by_id.pop(agent_id_str, None)
                # Stop tracking state-change history for dropped agents too; otherwise
                # an agent re-created with the same id later would appear to "change
                # state" relative to the stale tracked record.
                self._last_tracked_state_by_id.pop(agent_id_str, None)

            # Update last-known details with the synthesized UNKNOWN versions so
            # subsequent polls don't re-synthesize from the pre-UNKNOWN details.
            for unknown_agent in unknown_agents:
                self._last_known_details_by_id[str(unknown_agent.id)] = unknown_agent

        emitted_agents = tuple(agents) + tuple(unknown_agents)

        # Detect state changes against `_last_tracked_state_by_id`
        state_changes: list[tuple[AgentDetails, str | None, str | None]] = []
        with self._lock:
            for agent in emitted_agents:
                agent_id_str = str(agent.id)
                new_agent_state = agent.state.value
                new_host_state = agent.host.state.value if agent.host.state is not None else None
                tracked = self._last_tracked_state_by_id.get(agent_id_str)
                old_agent_state = tracked.agent_state if tracked else None
                old_host_state = tracked.host_state if tracked else None
                if old_agent_state != new_agent_state or old_host_state != new_host_state:
                    state_changes.append((agent, old_agent_state, old_host_state))
                    self._last_tracked_state_by_id[agent_id_str] = _TrackedState(
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
        # main_pid), and close watchers for agents dropped from tracking. The
        # synthesized UNKNOWN agents are intentionally left untouched: their
        # provider is unreachable, so the last-known watcher (if any) stays as-is.
        for agent in agents:
            self._reconcile_watcher_for_agent(agent)
        for agent_id_str in ids_to_drop:
            self._close_watcher(agent_id_str)

    # === PID Watchers (local agents only) ===

    def _reconcile_watcher_for_agent(self, agent: AgentDetails) -> None:
        """Open, replace, or close the PID watcher for one agent from its probed details.

        Only local agents are watched: a remote agent's ``main_pid`` is a PID in
        the remote host's namespace, so watching it here would watch an unrelated
        same-numbered local process. A remote agent, or a local agent with no
        ``main_pid`` (no longer running), closes any existing watcher.
        """
        agent_id_str = str(agent.id)
        if not agent.host.is_local or agent.main_pid is None:
            self._close_watcher(agent_id_str)
            return
        self._open_or_replace_watcher(agent_id_str, str(agent.host.id), agent.main_pid)

    def _open_or_replace_watcher(self, agent_id_str: str, host_id_str: str, pid: int) -> None:
        """Ensure a watcher thread is running for ``pid``, replacing one on a stale PID.

        Held under ``_watchers_lock`` for its whole duration so two reconcile paths
        (the activity worker and the snapshot loop) cannot each start a thread for
        the same agent and leak one. The stale-watcher stop/join is inlined rather
        than delegated to ``_close_watcher`` to avoid re-acquiring the non-reentrant
        lock; joining here is deadlock-free because ``_watch_pid`` never takes it.
        """
        with self._watchers_lock:
            existing = self._watchers.get(agent_id_str)
            if existing is not None and existing.pid == pid:
                return
            # New agent or the main process changed (PID differs): stop the stale
            # watcher first, then start a fresh one bound to the current PID.
            if existing is not None:
                self._watchers.pop(agent_id_str, None)
                existing.stop_event.set()
                existing.thread.join(timeout=5.0)
            try:
                process = psutil.Process(pid)
            except psutil.NoSuchProcess:
                # The process is already gone. Enqueue a re-probe so the next listing
                # emits the stopped/done state rather than silently missing the death.
                self._activity_queue.put(host_id_str)
                return
            stop_event = threading.Event()
            # is_checked=False so a single watcher's failure is isolated (logged via
            # on_failure) instead of being re-raised at the next strand start / group
            # exit, which would poison the whole ConcurrencyGroup and stop all
            # observation -- see _on_watcher_failure for the intended isolation.
            thread = self._concurrency_group.start_new_thread(
                target=lambda: self._watch_pid(agent_id_str, host_id_str, process, pid, stop_event),
                daemon=True,
                name=f"observe-pid-watch-{agent_id_str[:8]}",
                on_failure=self._on_watcher_failure,
                is_checked=False,
            )
            self._watchers[agent_id_str] = _AgentWatcher(pid=pid, stop_event=stop_event, thread=thread)

    def _watch_pid(
        self,
        agent_id_str: str,
        host_id_str: str,
        process: psutil.Process,
        pid: int,
        stop_event: threading.Event,
    ) -> None:
        """Block until the watched process exits (or a stop is requested), then signal activity.

        psutil implements ``wait`` event-driven (os.pidfd_open on Linux, kqueue on
        macOS), so death is noticed within milliseconds; the short per-call timeout
        exists only to re-check the stop flags, since an in-flight wait cannot be
        interrupted.
        """
        while not (stop_event.is_set() or self._stop_event.is_set()):
            try:
                process.wait(timeout=_WATCH_POLL_SECONDS)
            except psutil.TimeoutExpired:
                continue
            except (psutil.Error, OSError) as e:
                # psutil.Process.wait() can surface a bare OSError (not a psutil.Error)
                # when its underlying os.pidfd_open/kqueue/poll fails; treat any such
                # failure the same as an exit and re-probe rather than crash the watcher.
                logger.debug("PID watch for agent {} (pid {}) errored, treating as exit: {}", agent_id_str, pid, e)
            # Reached once the process has exited (wait returned) or errored out.
            logger.debug(
                "Local agent {} main process (pid {}) exited; enqueueing host {} for re-probe",
                agent_id_str,
                pid,
                host_id_str,
            )
            self._activity_queue.put(host_id_str)
            return

    def _close_watcher(self, agent_id_str: str) -> None:
        """Stop and join the watcher for an agent, if any. Idempotent.

        Held under ``_watchers_lock`` through the join (deadlock-free because the
        watcher thread never takes that lock) so it cannot race a concurrent
        reconcile into leaving two entries for the same agent.
        """
        with self._watchers_lock:
            watcher = self._watchers.pop(agent_id_str, None)
            if watcher is None:
                return
            watcher.stop_event.set()
            watcher.thread.join(timeout=5.0)

    def _close_all_watchers(self) -> None:
        """Tear down every PID watcher (observer shutdown)."""
        with self._watchers_lock:
            agent_id_strs = list(self._watchers.keys())
        for agent_id_str in agent_id_strs:
            self._close_watcher(agent_id_str)

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

    def _emit_agent_removed(self, agent_id: AgentId, agent_name: AgentName) -> None:
        """Emit an AGENT_REMOVED event to the agents stream for a destroyed agent."""
        event = make_agent_removed_event(agent_id, agent_name)
        self._emit_observe_event(event)
        logger.debug("Emitted agent removed event for {} ({})", agent_name, agent_id)

    def _emit_agent_state(self, agent: AgentDetails) -> None:
        """Emit a single agent state event, check for state/host state change, and update tracking."""
        event = make_agent_state_event(agent)
        self._emit_observe_event(event)
        logger.debug("Emitted agent state event for {} (state={})", agent.name, agent.state.value)

        agent_id_str = str(agent.id)
        new_agent_state = agent.state.value
        new_host_state = agent.host.state.value if agent.host.state is not None else None

        with self._lock:
            tracked = self._last_tracked_state_by_id.get(agent_id_str)
            old_agent_state = tracked.agent_state if tracked else None
            old_host_state = tracked.host_state if tracked else None
            self._last_tracked_state_by_id[agent_id_str] = _TrackedState(
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
