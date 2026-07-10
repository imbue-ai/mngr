"""Minds-side wrapper around the ``mngr forward`` plugin subprocess.

Phase 2 deletes minds' in-process subdomain-forwarding, auth, and observe-
spawning code; this file replaces them with a thin consumer that:

- spawns ``mngr forward --observe-via-file`` as a subprocess so it tails the
  shared discovery events file written by the single ``mngr observe`` under
  ``mngr latchkey forward`` instead of running its own observe;
- reads stdout line-by-line on a background thread and parses each line as a
  ``ForwardEnvelope``;
- dispatches by ``stream``: ``observe`` lines are folded into a shared
  ``DiscoveryStateAggregator`` (the span-aware, per-provider reconciler), whose
  view is then pushed into the surviving ``MngrCliBackendResolver`` and fanned
  out to a set of ``on_agent_discovered`` / ``on_agent_destroyed`` callbacks;
  ``event`` lines drive the resolver's
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
from imbue.mngr.api.discovery_aggregator import AggregatorDelta
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"
_PREAUTH_TOKEN_LENGTH: Final[int] = 64

OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]
OnSystemInterfaceBackendFailureCallback = Callable[[AgentId, SystemInterfaceBackendFailureReason, int | None], None]
OnUnexpectedExitCallback = Callable[[int], None]


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
        default=("has(agent.labels.is_primary)",),
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
    # The shared span-aware, per-provider discovery reconciler. Every parsed
    # observe event is folded into it; its accumulated view is what we push into
    # the resolver and fan out as discovered/destroyed callbacks.
    _aggregator: DiscoveryStateAggregator = PrivateAttr(default_factory=DiscoveryStateAggregator)
    # SSH connection info keyed by host id. The aggregator does not model SSH info
    # (it carries no agent/host membership), so it is tracked here and joined onto
    # the agents on each host when building the resolver's view.
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
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
        # The legacy global snapshot is superseded by per-provider snapshots, which
        # the shared aggregator reconciles; minds drops the legacy event here too
        # (consuming both would double-count agents during the transition window).
        if isinstance(event, FullDiscoverySnapshotEvent):
            logger.trace("Ignoring legacy full discovery snapshot; minds consumes per-provider snapshots")
            return
        # SSH info carries no agent/host membership, so the aggregator does not
        # model it; record it here before folding the event in so the agents view
        # we push below joins the info onto the agents on this host.
        if isinstance(event, HostSSHInfoEvent):
            self._record_host_ssh_info(event)
        delta = self._aggregator.apply_event(event)
        if isinstance(event, ProviderDiscoverySnapshotEvent):
            # A per-provider snapshot is also a discovery event, so update_providers
            # bumps last_event_at; merge just this provider's state + freshness.
            self.resolver.update_providers(
                provider_name=event.provider_name,
                provider=event.provider,
                error=event.error,
                last_snapshot_at=event.discovery_finished_at,
            )
        else:
            self._record_incremental_event(event)
        self._push_agents_view()
        self._fire_membership_delta(delta)
        # A HostSSHInfoEvent changes no membership (empty delta), so the agents on
        # the host were already announced without SSH info; re-announce them now
        # that their host's SSH info is known.
        if isinstance(event, HostSSHInfoEvent):
            self._refire_discovered_for_host(str(event.host_id))

    def _record_incremental_event(self, event: DiscoveryEvent) -> None:
        """Log a discovery error (if any) and bump the resolver's last-event time for a non-snapshot event.

        Incremental events (agent/host discovered or destroyed, SSH info, errors)
        are not snapshots, so they advance only ``last_event_at`` -- the
        per-provider snapshot freshness is bumped solely by ``update_providers``.
        """
        if isinstance(event, DiscoveryErrorEvent):
            logger.warning(
                "Discovery error from {}: {} ({})", event.source_name, event.error_message, event.error_type
            )
        self.resolver.record_discovery_event_received(datetime.now(timezone.utc))

    def _build_agents_result(self) -> ParsedAgentsResult:
        """Project the aggregator's agents + hosts (joined with SSH info) into a ParsedAgentsResult.

        The aggregator is the source of truth for agent / host membership and host
        lifecycle state. SSH info is tracked here (the aggregator does not model
        it) and joined onto each agent via its host id.
        """
        agents = self._aggregator.get_agents()
        hosts = self._aggregator.get_hosts()
        with self._lock:
            ssh_by_host_id = dict(self._ssh_by_host_id)
        ssh_info_by_agent_id = {
            str(agent.agent_id): ssh_by_host_id[str(agent.host_id)]
            for agent in agents
            if str(agent.host_id) in ssh_by_host_id
        }
        host_state_by_host_id = {str(host.host_id): host.host_state for host in hosts if host.host_state is not None}
        # Capture every known host's normalized name (regardless of whether its state
        # is known) so the resolver's display-name fallback and host-name collision
        # checks have it.
        host_name_by_host_id = {str(host.host_id): str(host.host_name) for host in hosts}
        return ParsedAgentsResult(
            agent_ids=tuple(agent.agent_id for agent in agents),
            discovered_agents=tuple(agents),
            ssh_info_by_agent_id=ssh_info_by_agent_id,
            host_state_by_host_id=host_state_by_host_id,
            host_name_by_host_id=host_name_by_host_id,
        )

    def _push_agents_view(self) -> None:
        """Push the aggregator's current agents / hosts view into the resolver."""
        self.resolver.update_agents(self._build_agents_result())

    def _fire_membership_delta(self, delta: AggregatorDelta) -> None:
        """Fire destroyed / discovered callbacks for the agents the delta removed or added.

        A removed agent also has its accumulated service map cleared from both this
        consumer and the resolver. A discovered agent's SSH info is looked up from
        its host (populated by a prior ``HostSSHInfoEvent``, if any). A removed
        host's cached SSH info is forgotten so the map does not grow without bound.
        """
        agent_by_id = self._aggregator.get_agent_by_id()
        for host_id_str in delta.removed_host_ids:
            with self._lock:
                self._ssh_by_host_id.pop(host_id_str, None)
        for agent_id_str in delta.removed_agent_ids:
            agent_id = AgentId(agent_id_str)
            with self._lock:
                self._services_by_agent.pop(agent_id_str, None)
            self.resolver.update_services(agent_id, {})
            self._fire_destroyed(agent_id)
        for agent_id_str in delta.added_agent_ids:
            agent = agent_by_id.get(agent_id_str)
            if agent is None:
                continue
            self._fire_discovered(agent.agent_id, self._ssh_for_host(str(agent.host_id)), str(agent.provider_name))

    def _record_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        """Store the SSH connection info carried by a HostSSHInfoEvent, keyed by host id."""
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user, host=event.ssh.host, port=event.ssh.port, key_path=event.ssh.key_path
        )
        with self._lock:
            self._ssh_by_host_id[str(event.host_id)] = ssh_info

    def _refire_discovered_for_host(self, host_id_str: str) -> None:
        """Re-announce every agent on a host, carrying the host's now-known SSH info."""
        ssh_info = self._ssh_for_host(host_id_str)
        for agent in self._aggregator.get_agents():
            if str(agent.host_id) == host_id_str:
                self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _ssh_for_host(self, host_id_str: str) -> RemoteSSHInfo | None:
        """Return the SSH connection info recorded for ``host_id_str``, or None."""
        with self._lock:
            return self._ssh_by_host_id.get(host_id_str)

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
    command = _build_forward_command(config, preauth_cookie)
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


def _build_forward_command(config: ForwardSubprocessConfig, preauth_cookie: str) -> list[str]:
    """Build the ``mngr forward`` argv for the subprocess minds spawns."""
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
    # TLS + HTTP/2 so the workspace origin is not capped by Chromium's
    # per-origin HTTP/1.1 connection limit. The Electron shell trusts the
    # proxy's self-signed cert for its loopback origins.
    command.append("--use-http2")
    for include in config.agent_include:
        command.extend(["--agent-include", include])
    for spec in config.reverse_specs:
        command.extend(["--reverse", spec])
    return command


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
