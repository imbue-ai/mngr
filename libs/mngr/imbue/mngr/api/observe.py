import fcntl
import json
import os
import queue
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
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
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
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


# === Event Types ===


class ObserveEventType(UpperCaseStrEnum):
    """Type of agent observation event."""

    AGENT_STATE = auto()
    AGENTS_FULL_STATE = auto()
    AGENT_STATE_CHANGE = auto()


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

    _concurrency_group: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="agent-observer"))
    _known_hosts: dict[str, _KnownHost] = PrivateAttr(default_factory=dict)
    _discovery_stream_process: RunningProcess = PrivateAttr(default_factory=dict)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _last_tracked_state_by_id: dict[str, _TrackedState] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
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
    # Union of providers + error_by_provider_name from the most recent snapshot.
    # When a previously-tracked agent's provider is no longer in this set, the
    # observer treats it as implicit destroy (config-removal) and drops the
    # agent from tracking instead of marking it UNKNOWN.
    _known_provider_names: set[ProviderInstanceName] = PrivateAttr(default_factory=set)
    # Set true on receipt of a DiscoveryErrorEvent with provider_name=None
    # (polling loop crashed entirely); cleared on the next successful snapshot.
    # While true, every tracked agent is considered UNKNOWN-eligible.
    _polling_loop_crashed: bool = PrivateAttr(default=False)
    # Triggered to wake the periodic-snapshot loop early when a DiscoveryErrorEvent
    # arrives, so UNKNOWN state propagates without waiting for the full poll interval.
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
        """Handle a line of output from 'mngr observe --discovery-only'."""
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return

        event = parse_discovery_event_line(stripped)

        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
        elif isinstance(event, HostDestroyedEvent):
            self._handle_host_destroyed(event)
        elif isinstance(event, DiscoveryErrorEvent):
            self._handle_discovery_error_event(event)
        else:
            pass

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Update known hosts and provider error state from a full discovery snapshot."""
        # Build the new set of known hosts from the host records (which carry the
        # authoritative host_name, unlike DiscoveredAgent which only has agent_name)
        new_hosts: dict[str, _KnownHost] = {}
        for host in event.hosts:
            host_id_str = str(host.host_id)
            new_hosts[host_id_str] = _KnownHost(
                host_id=host.host_id,
                host_name=host.host_name,
            )

        # The snapshot is authoritative state for which providers are currently
        # loaded and which of those are errored. A successful snapshot (even one
        # carrying some per-provider errors) also clears the "polling loop
        # crashed entirely" signal.
        new_known_providers = {p.provider_name for p in event.providers} | set(event.error_by_provider_name.keys())
        new_errored_providers = set(event.error_by_provider_name.keys())
        had_new_provider_errors = bool(event.error_by_provider_name)

        with self._lock:
            previously_known = set(self._known_hosts.keys())
            self._known_hosts = new_hosts
            self._known_provider_names = new_known_providers
            self._currently_errored_providers = new_errored_providers
            self._polling_loop_crashed = False

        new_host_ids = set(new_hosts.keys())

        # Stop streams for hosts that are no longer known
        for host_id_str in previously_known - new_host_ids:
            self._stop_activity_stream(host_id_str)

        # Start streams for newly discovered hosts
        for host_id_str in new_host_ids - previously_known:
            host = new_hosts[host_id_str]
            self._start_activity_stream(host_id_str, host.host_name)

        # Wake the periodic loop so UNKNOWN state propagates without waiting
        # for the full poll interval whenever provider errors are observed.
        if had_new_provider_errors:
            self._snapshot_trigger.set()

    def _handle_discovery_error_event(self, event: DiscoveryErrorEvent) -> None:
        """React to an incremental DiscoveryErrorEvent between snapshots.

        ``provider_name`` set: mark that provider's agents as UNKNOWN-eligible.
        ``provider_name`` is ``None``: the polling loop itself crashed, so every
        currently-tracked agent becomes UNKNOWN-eligible until the next
        successful snapshot.
        """
        if event.provider_name is None:
            with self._lock:
                self._polling_loop_crashed = True
        else:
            with self._lock:
                self._currently_errored_providers.add(ProviderInstanceName(event.provider_name))
        # Trigger a state snapshot so UNKNOWN propagates within seconds.
        self._snapshot_trigger.set()

    def _handle_host_destroyed(self, event: HostDestroyedEvent) -> None:
        """Remove a destroyed host from known hosts and stop its activity stream."""
        host_id_str = str(event.host_id)
        with self._lock:
            self._known_hosts.pop(host_id_str, None)
        self._stop_activity_stream(host_id_str)

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
            polling_crashed = self._polling_loop_crashed

            for agent_id_str, last_details in self._last_known_details_by_id.items():
                if agent_id_str in live_agent_ids:
                    continue
                provider = last_details.host.provider_name
                # Config removal trumps everything: provider no longer in any current set.
                # Skip this rule if we don't yet have a known-provider list (first snapshot).
                if known_providers and provider not in known_providers:
                    ids_to_drop.append(agent_id_str)
                    continue
                # Provider currently errored, or polling crashed -- synthesize UNKNOWN.
                if polling_crashed or provider in errored_providers:
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
        append_observe_event(self.events_base_dir, event)
        logger.debug(
            "Emitted full agent state event with {} agent(s) ({} live, {} unknown)",
            len(emitted_agents),
            len(agents),
            len(unknown_agents),
        )

        # Emit state change events to the agent_states stream
        for agent, old_agent_state, old_host_state in state_changes:
            self._emit_state_change(agent, old_agent_state, old_host_state)

    def _emit_agent_state(self, agent: AgentDetails) -> None:
        """Emit a single agent state event, check for state/host state change, and update tracking."""
        event = make_agent_state_event(agent)
        append_observe_event(self.events_base_dir, event)
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
