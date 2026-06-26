"""Minds-side wrapper around the ``mngr forward`` plugin subprocess.

Phase 2 deletes minds' in-process subdomain-forwarding, auth, and observe-
spawning code; this file replaces them with a thin consumer that:

- spawns ``mngr forward --observe-via-file`` as a subprocess so it tails the
  shared discovery events file written by the single ``mngr observe`` under
  ``mngr latchkey forward`` instead of running its own observe;
- reads stdout line-by-line on a background thread and parses each line as a
  ``ForwardEnvelope``;
- dispatches by ``stream``: ``observe`` lines drive the surviving
  ``MngrCliBackendResolver`` plus a set of ``on_agent_discovered`` /
  ``on_agent_destroyed`` callbacks; ``event`` lines drive the resolver's
  service map and fan out to request callbacks; ``forward`` lines
  feed the ``system_interface_backend_failure`` health tracker and the
  ``listening`` port handshake;
- watches the subprocess for premature exit and reports it (the consumer is
  dead, so the discovery pipeline is down) to the discovery-health watchdog
  via registered ``on_unexpected_exit`` callbacks.

Provider-set changes are picked up by bouncing the detached ``mngr latchkey
forward`` supervisor (the single discovery observer); the tailer here then sees
the fresh snapshot automatically, so this consumer no longer sends ``SIGHUP``.
"""

import json
import os
import secrets
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import SERVICES_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import ServiceDeregisteredRecord
from imbue.minds.desktop_client.backend_resolver import parse_service_log_record
from imbue.minds.errors import EnvelopeStreamConsumerError
from imbue.minds.utils.secret_redaction import redact_secret_flag_values
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.discovery_events import partition_removed_agents_by_provider_error
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostState
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"
_PREAUTH_TOKEN_LENGTH: Final[int] = 64

OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]
OnSystemInterfaceBackendFailureCallback = Callable[[AgentId, SystemInterfaceBackendFailureReason, int | None], None]
OnUnexpectedExitCallback = Callable[[int], None]


def _full_snapshot_observed_at(event: FullDiscoverySnapshotEvent) -> datetime:
    """Producer-side poll time of a full snapshot, for freshness comparisons.

    The envelope ``timestamp`` is stamped when the discovery producer finished the
    poll, so the host states the snapshot carries were observed at that instant.
    Minds gates the recovery redirect on whether a snapshot postdates an outage's
    onset, so it must compare against *when discovery observed the world*, not when
    minds happened to receive the line -- using the producer time removes the
    receive-tail-latency slop between the two. The producer and this consumer run
    on the same machine, so the timestamps share a clock. Falls back to the receive
    time if the envelope timestamp is unparseable (which only loosens the gate back
    to the prior receive-time behavior).
    """
    try:
        return datetime.fromisoformat(event.timestamp)
    except ValueError:
        logger.warning(
            "Full discovery snapshot carried an unparseable timestamp {!r}; using receive time", event.timestamp
        )
        return datetime.now(timezone.utc)


class ForwardSubprocessConfig(FrozenModel):
    """Args for the ``mngr forward`` subprocess that ``minds run`` spawns.

    Note: the preauth cookie is *not* a configurable field. It is freshly
    generated inside ``start_mngr_forward`` (so each run has a fresh
    secret) and returned to the caller as the second element of the
    tuple. Callers hand it to the Electron shell, which pre-sets
    ``mngr_forward_session=<value>`` on ``localhost:<port>``.
    """

    service: str = Field(default="system_interface", description="Service name to forward")
    agent_include: tuple[str, ...] = Field(
        default=("has(agent.labels.workspace) && has(agent.labels.is_primary)",),
        description="CEL include filters passed to --agent-include",
    )
    reverse_specs: tuple[str, ...] = Field(
        default=(),
        description="--reverse REMOTE:LOCAL pairs to set up",
    )
    mngr_binary: str = Field(default=MNGR_BINARY, description="Path to mngr binary")
    mngr_host_dir: Path = Field(default=_DEFAULT_MNGR_HOST_DIR, description="MNGR_HOST_DIR for the subprocess")


class EnvelopeStreamConsumer(MutableModel):
    """Owns the ``mngr forward`` subprocess and dispatches its envelope JSONL stream.

    Every public method is safe to call from minds' request-handling threads;
    internal state is guarded by ``_lock``.
    """

    resolver: MngrCliBackendResolver = Field(frozen=True, description="Resolver to feed observe + event lines into")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _host_state_by_host_id: dict[str, HostState] = PrivateAttr(default_factory=dict)
    _discovered_agents: dict[str, DiscoveredAgent] = PrivateAttr(default_factory=dict)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _on_system_interface_backend_failure_callbacks: list[OnSystemInterfaceBackendFailureCallback] = PrivateAttr(
        default_factory=list
    )
    _on_unexpected_exit_callbacks: list[OnUnexpectedExitCallback] = PrivateAttr(default_factory=list)
    _process: subprocess.Popen[bytes] | None = PrivateAttr(default=None)
    _has_reported_exit: bool = PrivateAttr(default=False)
    _intentional_shutdown: bool = PrivateAttr(default=False)
    # Set once the plugin emits its `listening` envelope; `_listening_port`
    # then holds the port the plugin actually bound. `wait_for_listening`
    # blocks on the event so `minds run` can learn the port at startup.
    _listening_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _listening_port: int | None = PrivateAttr(default=None)
    # Mirror of the plugin's per-agent ``ForwardResolver`` service map, fed by
    # ``resolver_snapshot`` envelopes. Used by minds' recovery-diagnostics path
    # to render Q7 (whether the plugin has seen the agent's system_interface).
    # Empty dict on a fresh / restarted plugin until the first envelope arrives.
    _resolver_snapshot_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)

    # -- Public callback registration -------------------------------------

    def add_on_agent_discovered_callback(self, callback: OnAgentDiscoveredCallback) -> None:
        """Register a callback fired for every observe-stream agent discovery."""
        with self._lock:
            self._on_agent_discovered_callbacks.append(callback)

    def add_on_agent_destroyed_callback(self, callback: OnAgentDestroyedCallback) -> None:
        """Register a callback fired for every observe-stream agent destruction."""
        with self._lock:
            self._on_agent_destroyed_callbacks.append(callback)

    def add_on_system_interface_backend_failure_callback(
        self, callback: OnSystemInterfaceBackendFailureCallback
    ) -> None:
        """Register a callback fired for each ``system_interface_backend_failure`` forward-stream envelope.

        The callback receives ``(agent_id, reason, status_code)``. ``reason``
        is a ``SystemInterfaceBackendFailureReason`` enum value (CONNECT_ERROR /
        SSE_EOF / ERROR_RESPONSE / UNRESOLVED); ``status_code`` is set when
        ``reason`` is ``ERROR_RESPONSE`` (the backend's non-2xx status) and
        ``None`` otherwise.
        Used by minds to feed its ``SystemInterfaceHealthTracker``.
        """
        with self._lock:
            self._on_system_interface_backend_failure_callbacks.append(callback)

    def add_on_unexpected_exit_callback(self, callback: OnUnexpectedExitCallback) -> None:
        """Register a callback fired once when the plugin subprocess exits unexpectedly.

        The callback receives the subprocess exit code. It fires only for an
        exit minds did not ask for (i.e. not after :meth:`terminate`), and at
        most once per consumer. The discovery-health watchdog registers here so
        a dead consumer transitions the app-global state straight to BLOCKED.
        """
        with self._lock:
            self._on_unexpected_exit_callbacks.append(callback)

    # -- Subprocess lifecycle ---------------------------------------------

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        """Store a freshly-spawned plugin subprocess.

        Reader threads are *not* started here -- callers must register
        every callback they need first, then call ``start()`` to begin
        consuming the envelope stream. This avoids a race where envelopes
        arriving between thread start and callback registration would be
        dispatched against an empty callback list.
        """
        if self._process is not None:
            raise EnvelopeStreamConsumerError("EnvelopeStreamConsumer.attach already called")
        self._process = process

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the reader / lifecycle threads for the attached subprocess.

        Must be called after ``attach()`` and after any callbacks that
        need to see the very first envelope have been registered.
        """
        if self._process is None:
            raise EnvelopeStreamConsumerError("EnvelopeStreamConsumer.start called before attach")
        concurrency_group.start_new_thread(
            target=self._read_stdout_loop,
            name="mngr-forward-stdout-reader",
            daemon=True,
            is_checked=False,
        )
        concurrency_group.start_new_thread(
            target=self._read_stderr_loop,
            name="mngr-forward-stderr-reader",
            daemon=True,
            is_checked=False,
        )
        concurrency_group.start_new_thread(
            target=self._wait_and_report_exit,
            name="mngr-forward-lifecycle-watcher",
            daemon=True,
            is_checked=False,
        )

    def wait_for_listening(self, timeout: float) -> int | None:
        """Block until the plugin reports its bound port, or ``timeout`` elapses.

        The plugin emits a single ``listening`` envelope once its FastAPI app
        is ready, carrying the port it actually bound (which differs from the
        default when that port was already in use). Returns that port, or
        ``None`` if no ``listening`` envelope arrived within ``timeout``
        seconds -- e.g. the subprocess died during startup.

        Must be called after ``start()``; otherwise the reader thread that
        consumes the envelope stream is not running and this always times out.
        """
        if not self._listening_event.wait(timeout=timeout):
            return None
        with self._lock:
            return self._listening_port

    def terminate(self) -> None:
        """Stop the plugin subprocess (SIGTERM, then SIGKILL on timeout).

        Sets ``_intentional_shutdown`` *before* signalling the subprocess
        so the lifecycle watcher (``_wait_and_report_exit``) does not report
        the resulting exit to the watchdog as a dead pipeline.
        """
        process = self._process
        if process is None:
            return
        self._intentional_shutdown = True
        try:
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError as e:
            logger.trace("Error terminating plugin subprocess: {}", e)

    # -- Reader threads ---------------------------------------------------

    def _read_stdout_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            self._handle_envelope_line(line)

    def _read_stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            stripped = line.strip()
            if stripped:
                logger.debug("mngr forward stderr: {}", stripped)

    def _wait_and_report_exit(self) -> None:
        process = self._process
        if process is None:
            return
        exit_code = process.wait()
        # If minds asked the subprocess to stop (lifespan shutdown), the exit is
        # expected -- not a dead pipeline.
        if self._intentional_shutdown:
            logger.debug("mngr forward exited with code {} after intentional shutdown", exit_code)
            return
        # Any unasked-for exit means the consumer (and thus the discovery
        # pipeline + traffic proxy) is down. Report it once to the watchdog,
        # which owns the user-facing surfacing (the app-global BLOCKED screen).
        if self._has_reported_exit:
            return
        self._has_reported_exit = True
        logger.error("mngr forward exited unexpectedly with code {}", exit_code)
        with self._lock:
            callbacks = list(self._on_unexpected_exit_callbacks)
        for callback in callbacks:
            try:
                callback(exit_code)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_unexpected_exit callback failed: {}", e)

    # -- Envelope parsing + dispatch --------------------------------------

    def _handle_envelope_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse envelope line {!r}: {}", stripped[:200], e)
            return
        if not isinstance(envelope, dict):
            return
        stream = envelope.get("stream")
        agent_id_value = envelope.get("agent_id")
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return
        if stream == "observe":
            self._handle_observe_payload(payload)
        elif stream == "event":
            if isinstance(agent_id_value, str):
                self._handle_event_payload(AgentId(agent_id_value), payload)
        elif stream == "forward":
            self._handle_forward_payload(payload)
        else:
            logger.trace("Unknown envelope stream {!r}", stream)

    def _handle_observe_payload(self, payload: dict[str, Any]) -> None:
        # Re-serialize to a single-line JSON so we can reuse mngr's parser.
        try:
            line = json.dumps(payload, separators=(",", ":"))
            event = parse_discovery_event_line(line)
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse observe payload: {}", e)
            return
        if event is None:
            return
        # Snapshot handler bumps both last_event_at and last_full_snapshot_at
        # via update_providers; it does not flow through the
        # record_discovery_event_received path. Dispatch the snapshot first,
        # then bump last_event_at once for every other event type below.
        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
            return
        self.resolver.record_discovery_event_received(datetime.now(timezone.utc))
        if isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, HostDiscoveryEvent):
            self._handle_host_discovered(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, AgentDestroyedEvent):
            self._handle_agent_destroyed(event.agent_id)
        elif isinstance(event, HostDestroyedEvent):
            # Record the terminal state first so any snapshot that re-lists this
            # host during the destroyed-host persistence window is recognized as
            # destroyed, then tear down the agents that were on it.
            with self._lock:
                self._host_state_by_host_id[str(event.host_id)] = HostState.DESTROYED
                self._ssh_by_host_id.pop(str(event.host_id), None)
            for aid in event.agent_ids:
                self._handle_agent_destroyed(aid)
        elif isinstance(event, DiscoveryErrorEvent):
            logger.warning(
                "Discovery error from {}: {} ({})", event.source_name, event.error_message, event.error_type
            )
        else:
            # parse_discovery_event_line returns the union we already
            # exhaustively enumerated above; an unknown event type means
            # mngr added a new discovery type the plugin still passes
            # through. Log once at trace-level so it's visible without
            # being noisy.
            logger.trace("Ignoring unknown discovery event: {}", type(event).__name__)

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        # Agents present in this snapshot.
        fresh_agents: dict[str, DiscoveredAgent] = {}
        fresh_host_map: dict[str, str] = {}
        for agent in event.agents:
            fresh_agents[str(agent.agent_id)] = agent
            fresh_host_map[str(agent.agent_id)] = str(agent.host_id)
        with self._lock:
            prior_agents = dict(self._discovered_agents)
            prior_host_map = dict(self._agent_host_map)
            removed = prior_agents.keys() - fresh_agents.keys()
            # An agent absent because its provider errored is retained (kept in
            # the resolver and surfaced as stale via error_by_provider_name)
            # rather than dropped; only genuinely-removed agents fire destroyed.
            partition = partition_removed_agents_by_provider_error(
                removed_agent_ids=removed,
                provider_name_by_prior_agent_id={
                    aid_str: str(agent.provider_name) for aid_str, agent in prior_agents.items()
                },
                error_by_provider_name=event.error_by_provider_name,
            )
            merged_agents = dict(fresh_agents)
            merged_host_map = dict(fresh_host_map)
            for aid_str in partition.retained:
                merged_agents[aid_str] = prior_agents[aid_str]
                merged_host_map[aid_str] = prior_host_map[aid_str]
            self._discovered_agents = merged_agents
            self._agent_host_map = merged_host_map
            ssh_info_by_agent = {
                aid: self._ssh_by_host_id[hid] for aid, hid in merged_host_map.items() if hid in self._ssh_by_host_id
            }
            # Rebuild host state from this snapshot's hosts (a destroyed host
            # lingers here with host_state=DESTROYED for its persistence window).
            # Retained agents' hosts are absent from an errored provider's
            # snapshot, so carry their last-known state forward.
            prior_host_state = dict(self._host_state_by_host_id)
            merged_host_state: dict[str, HostState] = {
                str(host.host_id): host.host_state for host in event.hosts if host.host_state is not None
            }
            for aid_str in partition.retained:
                retained_host_id = merged_host_map[aid_str]
                if retained_host_id not in merged_host_state and retained_host_id in prior_host_state:
                    merged_host_state[retained_host_id] = prior_host_state[retained_host_id]
            self._host_state_by_host_id = merged_host_state
            host_state_snapshot = dict(merged_host_state)
        # Push the merged set (fresh + retained) so retained agents stay listed
        # in the workspace UI; their provider_name lets the workspace list mark
        # them stale by cross-referencing the errored providers below.
        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=tuple(agent.agent_id for agent in merged_agents.values()),
                discovered_agents=tuple(merged_agents.values()),
                ssh_info_by_agent_id=ssh_info_by_agent,
                host_state_by_host_id=host_state_snapshot,
            )
        )
        # Push provider state + freshness timestamp through the resolver so the
        # providers panel can render OK / Error badges, the workspace list can
        # mark retained agents stale, and the "time since last full discovery
        # event" counter updates -- without parsing the discovery stream itself.
        self.resolver.update_providers(
            providers=event.providers,
            error_by_provider_name=event.error_by_provider_name,
            last_full_snapshot_at=_full_snapshot_observed_at(event),
        )
        if partition.retained:
            logger.debug(
                "Retained {} agent(s) through a provider discovery error; surfacing as stale: {}",
                len(partition.retained),
                sorted(partition.retained),
            )
        for aid_str in partition.dropped:
            self._fire_destroyed(AgentId(aid_str))
        # Only (re)fire discovery for agents actually present in this snapshot;
        # retained agents were already announced and stay set up.
        for agent in fresh_agents.values():
            ssh_info = ssh_info_by_agent.get(str(agent.agent_id))
            self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _build_agents_result_locked(self) -> ParsedAgentsResult:
        """Snapshot the current agent/ssh/host-state maps into a ParsedAgentsResult.

        Must be called while ``self._lock`` is held: it reads the shared mutable
        maps. Callers push the result to the resolver *after* releasing the lock.
        Centralizing this keeps the per-event handlers from each re-deriving (and
        having to re-thread every new resolver-snapshot field through) the same
        merged view.
        """
        agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
        ssh_info_by_agent = {
            aid: self._ssh_by_host_id[hid] for aid, hid in self._agent_host_map.items() if hid in self._ssh_by_host_id
        }
        discovered = tuple(self._discovered_agents.values())
        host_state_snapshot = dict(self._host_state_by_host_id)
        return ParsedAgentsResult(
            agent_ids=agent_ids,
            discovered_agents=discovered,
            ssh_info_by_agent_id=ssh_info_by_agent,
            host_state_by_host_id=host_state_snapshot,
        )

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user, host=event.ssh.host, port=event.ssh.port, key_path=event.ssh.key_path
        )
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
            agents_on_host = [AgentId(aid) for aid, hid in self._agent_host_map.items() if hid == host_id_str]
            agents_result = self._build_agents_result_locked()
        self.resolver.update_agents(agents_result)
        for agent_id in agents_on_host:
            self._fire_discovered(agent_id, ssh_info, self._provider_name_for_agent(agent_id))

    def _handle_host_discovered(self, event: HostDiscoveryEvent) -> None:
        if event.host.host_state is None:
            return
        with self._lock:
            self._host_state_by_host_id[str(event.host.host_id)] = event.host.host_state
            agents_result = self._build_agents_result_locked()
        self.resolver.update_agents(agents_result)

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        agent = event.agent
        aid_str = str(agent.agent_id)
        with self._lock:
            self._agent_host_map[aid_str] = str(agent.host_id)
            self._discovered_agents[aid_str] = agent
            ssh_info = self._ssh_by_host_id.get(str(agent.host_id))
            agents_result = self._build_agents_result_locked()
        self.resolver.update_agents(agents_result)
        self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _handle_agent_destroyed(self, agent_id: AgentId) -> None:
        aid_str = str(agent_id)
        with self._lock:
            self._discovered_agents.pop(aid_str, None)
            self._agent_host_map.pop(aid_str, None)
            self._services_by_agent.pop(aid_str, None)
            agents_result = self._build_agents_result_locked()
        self.resolver.update_agents(agents_result)
        self.resolver.update_services(agent_id, {})
        self._fire_destroyed(agent_id)

    def _provider_name_for_agent(self, agent_id: AgentId) -> str:
        with self._lock:
            agent = self._discovered_agents.get(str(agent_id))
        if agent is None:
            return "unknown"
        return str(agent.provider_name)

    def _fire_discovered(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        with self._lock:
            callbacks = list(self._on_agent_discovered_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id, ssh_info, provider_name)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_discovered callback failed for {}: {}", agent_id, e)

    def _fire_destroyed(self, agent_id: AgentId) -> None:
        with self._lock:
            callbacks = list(self._on_agent_destroyed_callbacks)
        for callback in callbacks:
            try:
                callback(agent_id)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_destroyed callback failed for {}: {}", agent_id, e)

    # -- Per-agent event lines (services / requests) ----------------------

    def _handle_event_payload(self, agent_id: AgentId, payload: dict[str, Any]) -> None:
        source = payload.get("source", "")
        aid_str = str(agent_id)
        if source == REQUESTS_EVENT_SOURCE_NAME:
            raw_line = json.dumps(payload, separators=(",", ":"))
            self.resolver.fire_on_request(aid_str, raw_line)
            return
        if source != SERVICES_EVENT_SOURCE_NAME:
            return
        try:
            record = parse_service_log_record(payload)
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse service event for {}: {}", agent_id, e)
            return
        with self._lock:
            services = self._services_by_agent.setdefault(aid_str, {})
            if isinstance(record, ServiceDeregisteredRecord):
                services.pop(str(record.service), None)
            else:
                services[str(record.service)] = record.url
            services_snapshot = dict(services)
        self.resolver.update_services(agent_id, services_snapshot)

    # -- Forward-stream payloads ------------------------------------------

    def get_resolver_snapshot_for_agent(self, agent_id: AgentId) -> dict[str, str]:
        """Return the latest plugin-side service map for ``agent_id``.

        Returns an empty dict if no ``resolver_snapshot`` envelope has been
        seen for this agent yet (plugin restarted, or agent not yet
        published its services). The caller should treat the empty case
        as "no entry yet" -- it is not evidence of failure.
        """
        with self._lock:
            return dict(self._resolver_snapshot_by_agent.get(str(agent_id), {}))

    def _handle_resolver_snapshot(self, payload: dict[str, Any]) -> None:
        """Record the latest per-agent service map from a ``resolver_snapshot`` envelope."""
        services_by_agent = payload.get("services_by_agent")
        if not isinstance(services_by_agent, dict):
            logger.warning("Malformed resolver_snapshot envelope: {}", payload)
            return
        new_snapshot: dict[str, dict[str, str]] = {}
        for aid, services in services_by_agent.items():
            if not isinstance(aid, str) or not isinstance(services, dict):
                continue
            entry: dict[str, str] = {}
            for service_name, url in services.items():
                if isinstance(service_name, str) and isinstance(url, str):
                    entry[service_name] = url
            new_snapshot[aid] = entry
        with self._lock:
            self._resolver_snapshot_by_agent = new_snapshot

    def _handle_forward_payload(self, payload: dict[str, Any]) -> None:
        payload_type = payload.get("type")
        if payload_type == "reverse_tunnel_established":
            logger.trace("Ignoring reverse_tunnel_established envelope: {}", payload)
        elif payload_type == "resolver_snapshot":
            self._handle_resolver_snapshot(payload)
        elif payload_type == "system_interface_backend_failure":
            try:
                agent_id = AgentId(str(payload["agent_id"]))
                reason = SystemInterfaceBackendFailureReason(str(payload["reason"]))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Could not parse system_interface_backend_failure payload: {}", e)
                return
            raw_status_code = payload.get("status_code")
            try:
                status_code: int | None = int(raw_status_code) if raw_status_code is not None else None
            except (ValueError, TypeError):
                status_code = None
            with self._lock:
                callbacks = list(self._on_system_interface_backend_failure_callbacks)
            for callback in callbacks:
                try:
                    callback(agent_id, reason, status_code)
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("system_interface_backend_failure callback failed for {}: {}", agent_id, e)
        elif payload_type == "listening":
            self._handle_listening(payload)
        elif payload_type == "login_url":
            logger.debug("Forward stream payload {}: {}", payload_type, payload)
        else:
            logger.trace("Unknown forward payload type {!r}", payload_type)

    def _handle_listening(self, payload: dict[str, Any]) -> None:
        """Record the plugin's bound port from a ``listening`` envelope.

        Fires ``_listening_event`` so a ``wait_for_listening`` caller unblocks.
        A malformed payload is logged and dropped -- the waiter then times out
        rather than proceeding with a bogus port.
        """
        raw_port = payload.get("port")
        if raw_port is None:
            logger.warning("`listening` envelope is missing its port: {}", payload)
            return
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            logger.warning("Could not parse port from `listening` envelope: {}", payload)
            return
        with self._lock:
            self._listening_port = port
        self._listening_event.set()
        logger.info("`mngr forward` is listening on port {}", port)


# -- start_mngr_forward ----------------------------------------------------


def start_mngr_forward(
    config: ForwardSubprocessConfig,
    resolver: MngrCliBackendResolver,
) -> tuple[EnvelopeStreamConsumer, str]:
    """Spawn the ``mngr forward`` subprocess and attach an envelope consumer.

    Returns ``(consumer, preauth_cookie_value)``. The reader threads are
    *not* started yet -- the caller MUST:

    1. register its on_agent_discovered / on_agent_destroyed handlers
       on the consumer;
    2. call ``consumer.start(concurrency_group)`` to begin consuming
       envelopes;
    3. hand the preauth cookie to the Electron shell so it can pre-set
       ``mngr_forward_session=<value>`` on ``localhost:<port>`` before the
       first agent-subdomain navigation.

    Splitting attach (here) from start (caller) avoids a race where
    envelopes arriving before the caller has registered its callbacks
    would be dispatched against an empty callback list and silently
    dropped.
    """
    preauth_cookie = secrets.token_urlsafe(_PREAUTH_TOKEN_LENGTH)
    command: list[str] = [
        config.mngr_binary,
        "forward",
        "--host",
        "127.0.0.1",
        "--service",
        config.service,
        # Tail the shared discovery log written by the single `mngr observe` under
        # `mngr latchkey forward` rather than spawning a second discovery observer.
        "--observe-via-file",
        "--preauth-cookie",
        preauth_cookie,
        "--format",
        "jsonl",
    ]
    for include in config.agent_include:
        command.extend(["--agent-include", include])
    for spec in config.reverse_specs:
        command.extend(["--reverse", spec])
    env = dict(os.environ)
    env["MNGR_HOST_DIR"] = str(config.mngr_host_dir)
    logger.info("Spawning `mngr forward` subprocess: {}", " ".join(_redact_secrets(command)))
    # noqa: S603 — command is fully controlled (mngr binary + the args we
    # build above), no untrusted input reaches the argv list.
    process = subprocess.Popen(  # noqa: S603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env,
        cwd=str(Path.home()),
    )
    consumer = EnvelopeStreamConsumer(resolver=resolver)
    consumer.attach(process)
    return consumer, preauth_cookie


def _redact_secrets(command: list[str]) -> list[str]:
    """Return a copy of ``command`` with secret-bearing argument values masked for logging.

    The actual ``Popen`` call uses the unredacted list so the plugin still
    receives the real values. Today we redact the ``--preauth-cookie`` value
    (a freshly-minted shared secret between minds, the plugin, and the
    Electron shell); future secret-bearing flags can be added to
    ``_SECRET_BEARING_FLAGS``.
    """
    return redact_secret_flag_values(command, secret_bearing_flags=_SECRET_BEARING_FLAGS)


_SECRET_BEARING_FLAGS: Final[tuple[str, ...]] = ("--preauth-cookie",)
