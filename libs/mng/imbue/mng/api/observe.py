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
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mng.api.discovery_events import parse_discovery_event_line
from imbue.mng.api.list import list_agents
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostName

# === Constants ===

OBSERVE_EVENT_SOURCE: Final[EventSource] = EventSource("mng/agents")
AGENT_STATES_EVENT_SOURCE: Final[EventSource] = EventSource("mng/agent_states")
ACTIVITY_EVENT_SOURCE: Final[EventSource] = EventSource("mng/activity")
OBSERVE_LOCK_FILENAME: Final[str] = "observe_lock"
FULL_STATE_INTERVAL_SECONDS: Final[float] = 300.0
_ACTIVITY_DEBOUNCE_SECONDS: Final[float] = 2.0


def _event_source_filename(source: EventSource) -> str:
    """Build the events JSONL filename for a given event source."""
    return f"{source}/events.jsonl"


# === Event Types ===


class ObserveEventType(UpperCaseStrEnum):
    """Type of agent observation event."""

    AGENT_STATE = auto()
    AGENTS_FULL_STATE = auto()
    AGENT_STATE_CHANGE = auto()


class AgentStateEvent(EventEnvelope):
    """An individual agent's current state, emitted when activity is detected on its host."""

    agent: dict = Field(description="Serialized AgentDetails for the agent")


class FullAgentStateEvent(EventEnvelope):
    """Full state snapshot of all known agents."""

    agents: tuple[dict, ...] = Field(description="Serialized AgentDetails for all known agents")


class AgentStateChangeEvent(EventEnvelope):
    """Emitted when an agent's lifecycle state field changes.

    Written to the agent_states event stream, separate from the main agents stream.
    """

    agent_id: AgentId = Field(description="ID of the agent whose state changed")
    agent_name: AgentName = Field(description="Name of the agent whose state changed")
    old_state: str | None = Field(description="Previous lifecycle state value, or None if first observation")
    new_state: str = Field(description="New lifecycle state value")
    agent: dict = Field(description="Full serialized AgentDetails at time of state change")


# === Path Helpers ===


@pure
def get_observe_events_dir(config: MngConfig) -> Path:
    """Return the directory for agent observation event files."""
    host_dir = Path(config.default_host_dir).expanduser()
    return host_dir / "events" / "mng" / "agents"


@pure
def get_observe_events_path(config: MngConfig) -> Path:
    """Return the path to the agent observation events JSONL file."""
    return get_observe_events_dir(config) / "events.jsonl"


@pure
def get_agent_states_events_dir(config: MngConfig) -> Path:
    """Return the directory for agent state change event files."""
    host_dir = Path(config.default_host_dir).expanduser()
    return host_dir / "events" / "mng" / "agent_states"


@pure
def get_agent_states_events_path(config: MngConfig) -> Path:
    """Return the path to the agent state change events JSONL file."""
    return get_agent_states_events_dir(config) / "events.jsonl"


@pure
def get_observe_lock_path(config: MngConfig) -> Path:
    """Return the path to the observe lock file."""
    host_dir = Path(config.default_host_dir).expanduser()
    return host_dir / OBSERVE_LOCK_FILENAME


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
        agent=agent_details.model_dump(mode="json"),
    )


def make_full_agent_state_event(agents: Sequence[AgentDetails]) -> FullAgentStateEvent:
    """Build a full state snapshot event for all known agents."""
    timestamp, event_id = _make_envelope_fields()
    return FullAgentStateEvent(
        timestamp=timestamp,
        type=EventType(ObserveEventType.AGENTS_FULL_STATE),
        event_id=event_id,
        source=OBSERVE_EVENT_SOURCE,
        agents=tuple(a.model_dump(mode="json") for a in agents),
    )


def make_agent_state_change_event(
    agent: AgentDetails,
    old_state: str | None,
) -> AgentStateChangeEvent:
    """Build an event recording a change in an agent's lifecycle state field."""
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
        agent=agent.model_dump(mode="json"),
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


def append_observe_event(config: MngConfig, event: EventEnvelope) -> None:
    """Append a single observation event to the agents JSONL file."""
    _append_event_to_file(get_observe_events_path(config), event)


def append_agent_state_change_event(config: MngConfig, event: AgentStateChangeEvent) -> None:
    """Append a state change event to the agent_states JSONL file."""
    _append_event_to_file(get_agent_states_events_path(config), event)


# === History Loading ===


def load_base_state_from_history(
    config: MngConfig,
) -> dict[str, str]:
    """Load base agent state from the most recent full state event in history.

    Scans the observe events file for the latest AGENTS_FULL_STATE event and
    reconstructs the last known lifecycle state for each agent.

    Returns a dict mapping agent ID -> lifecycle state value string.
    """
    events_path = get_observe_events_path(config)
    if not events_path.exists():
        return {}

    latest_agents_data: tuple[dict, ...] | None = None
    with open(events_path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as e:
                logger.trace("Skipping malformed line in event history: {}", e)
                continue
            if data.get("type") == ObserveEventType.AGENTS_FULL_STATE:
                latest_agents_data = tuple(data.get("agents", ()))

    if latest_agents_data is None:
        return {}

    last_agent_state_by_id: dict[str, str] = {}
    for agent_dict in latest_agents_data:
        agent_id = agent_dict.get("id")
        if agent_id is not None:
            state = agent_dict.get("state")
            if state is not None:
                last_agent_state_by_id[str(agent_id)] = str(state)

    return last_agent_state_by_id


# === Locking ===


class ObserveLockError(MngError):
    """Raised when another mng observe instance is already running."""

    def __init__(self) -> None:
        super().__init__("Another 'mng observe' instance is already running. Only one instance can run at a time.")


def acquire_observe_lock(config: MngConfig) -> int:
    """Acquire an exclusive file lock for the observe process.

    Returns the file descriptor (caller must keep it open to hold the lock).
    Raises ObserveLockError if another instance already holds the lock.
    """
    lock_path = get_observe_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ObserveLockError() from None
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


class AgentObserver(MutableModel):
    """Observes agent state changes across all hosts.

    Uses 'mng list --stream' to track hosts and 'mng events' to stream
    activity events from each online host. When activity is detected,
    fetches agent state and emits events to local JSONL files:

    - events/mng/agents/events.jsonl: individual and full agent state snapshots
    - events/mng/agent_states/events.jsonl: only when the lifecycle state field changes
    """

    mng_ctx: MngContext = Field(frozen=True)
    mng_binary: str = Field(default="mng", frozen=True)

    _cg: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="agent-observer"))
    _known_hosts: dict[str, _KnownHost] = PrivateAttr(default_factory=dict)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _last_agent_state_by_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _activity_queue: queue.Queue[str] = PrivateAttr(default_factory=queue.Queue)

    def run(self) -> None:
        """Run the observer. Blocks until stopped or interrupted."""
        with self._cg:
            # Load base state from event history so we can detect state changes since last run
            with log_span("Loading base state from history"):
                self._last_agent_state_by_id = load_base_state_from_history(self.mng_ctx.config)
                logger.debug(
                    "Loaded base state for {} agent(s) from history",
                    len(self._last_agent_state_by_id),
                )

            # Phase 1: initial full state snapshot
            with log_span("Performing initial full state snapshot"):
                self._do_full_state_snapshot()

            # Phase 2: start host discovery stream
            with log_span("Starting host discovery stream"):
                self._start_list_stream()

            # Phase 3: start the activity worker thread
            activity_worker = threading.Thread(
                target=self._activity_worker,
                daemon=True,
                name="observe-activity-worker",
            )
            activity_worker.start()

            # Phase 4: periodic full state snapshots + wait for stop
            try:
                while not self._stop_event.is_set():
                    self._stop_event.wait(timeout=FULL_STATE_INTERVAL_SECONDS)
                    if self._stop_event.is_set():
                        break
                    try:
                        with log_span("Performing periodic full state snapshot"):
                            self._do_full_state_snapshot()
                    except (MngError, OSError) as e:
                        logger.warning("Periodic full state snapshot failed (continuing): {}", e)
            except KeyboardInterrupt:
                pass
            finally:
                self._stop_event.set()
                activity_worker.join(timeout=5.0)

    def stop(self) -> None:
        """Signal the observer to stop."""
        self._stop_event.set()

    def _start_list_stream(self) -> None:
        """Start the 'mng list --stream' subprocess for host discovery."""
        self._cg.run_process_in_background(
            command=[self.mng_binary, "list", "--stream", "--quiet"],
            on_output=self._on_list_stream_output,
        )

    def _on_list_stream_output(self, line: str, is_stdout: bool) -> None:
        """Handle a line of output from 'mng list --stream'."""
        if not is_stdout:
            return
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = parse_discovery_event_line(stripped)
        except (json.JSONDecodeError, ValueError) as e:
            logger.trace("Failed to parse discovery event: {}", e)
            return

        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        """Update known hosts from a full discovery snapshot and sync activity streams."""
        # Build the new set of known hosts from the host records (which carry the
        # authoritative host_name, unlike DiscoveredAgent which only has agent_name)
        new_hosts: dict[str, _KnownHost] = {}
        for host in event.hosts:
            host_id_str = str(host.host_id)
            new_hosts[host_id_str] = _KnownHost(
                host_id=host.host_id,
                host_name=host.host_name,
            )

        with self._lock:
            previously_known = set(self._known_hosts.keys())
            self._known_hosts = new_hosts

        new_host_ids = set(new_hosts.keys())

        # Stop streams for hosts that are no longer known
        for host_id_str in previously_known - new_host_ids:
            self._stop_activity_stream(host_id_str)

        # Start streams for newly discovered hosts
        for host_id_str in new_host_ids - previously_known:
            host = new_hosts[host_id_str]
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
            process = self._cg.run_process_in_background(
                command=[
                    self.mng_binary,
                    "events",
                    str(host_name),
                    _event_source_filename(ACTIVITY_EVENT_SOURCE),
                    "--follow",
                    "--quiet",
                ],
                on_output=lambda line, is_stdout: self._on_activity_event(line, is_stdout, host_id_str),
            )
            with self._lock:
                self._events_processes[host_id_str] = process
        except (MngError, OSError) as e:
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
                except (MngError, OSError) as e:
                    logger.warning("Failed to fetch agent state for host {}: {}", hid, e)

    def _fetch_and_emit_agent_state_for_host(self, host_id_str: str) -> None:
        """Fetch current agent state for a host and emit events for all agents."""
        with self._lock:
            host = self._known_hosts.get(host_id_str)
        if host is None:
            return

        with log_span("Fetching agent state for host {}", host.host_name):
            result = list_agents(
                mng_ctx=self.mng_ctx,
                is_streaming=False,
                include_filters=(f'host.id == "{host.host_id}"',),
                error_behavior=ErrorBehavior.CONTINUE,
            )

        for agent in result.agents:
            self._emit_agent_state(agent)

    def _do_full_state_snapshot(self) -> None:
        """Perform a full listing, emit a full state event, and check for state changes."""
        result = list_agents(
            mng_ctx=self.mng_ctx,
            is_streaming=False,
            error_behavior=ErrorBehavior.CONTINUE,
        )

        if result.errors:
            for error in result.errors:
                logger.warning("Error during full state snapshot: {} - {}", error.exception_type, error.message)

        self._process_snapshot_agents(result.agents)

    def _process_snapshot_agents(self, agents: Sequence[AgentDetails]) -> None:
        """Process agents from a full snapshot: detect state changes, emit events, update tracking."""
        state_changes: list[tuple[AgentDetails, str | None]] = []
        with self._lock:
            for agent in agents:
                agent_id_str = str(agent.id)
                new_state = agent.state.value
                old_state = self._last_agent_state_by_id.get(agent_id_str)
                if old_state != new_state:
                    state_changes.append((agent, old_state))
                    self._last_agent_state_by_id[agent_id_str] = new_state

        # Emit the full state event (includes all agents regardless of change)
        event = make_full_agent_state_event(agents)
        append_observe_event(self.mng_ctx.config, event)
        logger.debug(
            "Emitted full agent state event with {} agent(s)",
            len(agents),
        )

        # Emit state change events to the agent_states stream
        for agent, old_state in state_changes:
            self._emit_state_change(agent, old_state)

    def _emit_agent_state(self, agent: AgentDetails) -> None:
        """Emit a single agent state event, check for state field change, and update tracking."""
        event = make_agent_state_event(agent)
        append_observe_event(self.mng_ctx.config, event)
        logger.debug("Emitted agent state event for {} (state={})", agent.name, agent.state.value)

        agent_id_str = str(agent.id)
        new_state = agent.state.value

        with self._lock:
            old_state = self._last_agent_state_by_id.get(agent_id_str)
            self._last_agent_state_by_id[agent_id_str] = new_state

        if old_state != new_state:
            self._emit_state_change(agent, old_state)

    def _emit_state_change(self, agent: AgentDetails, old_state: str | None) -> None:
        """Emit a state change event to the agent_states stream."""
        state_change_event = make_agent_state_change_event(agent, old_state)
        append_agent_state_change_event(self.mng_ctx.config, state_change_event)
        logger.debug(
            "Emitted agent state change for {} ({} -> {})",
            agent.name,
            old_state,
            agent.state.value,
        )
