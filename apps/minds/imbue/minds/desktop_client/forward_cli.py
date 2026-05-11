"""Minds-side wrapper around the ``mngr forward`` plugin subprocess.

Phase 2 deletes minds' in-process subdomain-forwarding, auth, and observe-
spawning code; this file replaces them with a thin consumer that:

- spawns ``mngr forward`` as a subprocess (via ``subprocess.Popen`` so we get
  direct access to the PID for ``SIGHUP``);
- reads stdout line-by-line on a background thread and parses each line as a
  ``ForwardEnvelope``;
- dispatches by ``stream``: ``observe`` lines drive the surviving
  ``MngrCliBackendResolver`` plus a set of ``on_agent_discovered`` /
  ``on_agent_destroyed`` callbacks; ``event`` lines drive the resolver's
  service map and fan out to request / refresh callbacks; ``forward`` lines
  fire ``on_reverse_tunnel_established`` for the ``MindsApiUrlWriter``;
- exposes ``bounce_observe()`` (sends ``SIGHUP`` to the plugin's PID), used
  by ``supertokens_routes`` after a freshly-written
  ``[providers.imbue_cloud_<slug>]`` block in ``settings.toml``;
- watches the subprocess for premature exit and surfaces stderr + exit code
  via ``NotificationDispatcher``.
"""

import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import REFRESH_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import REQUESTS_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import SERVICES_EVENT_SOURCE_NAME
from imbue.minds.desktop_client.backend_resolver import ServiceDeregisteredRecord
from imbue.minds.desktop_client.backend_resolver import parse_service_log_record
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import open_ssh_client
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent

_DEFAULT_MNGR_HOST_DIR: Final[Path] = Path.home() / ".mngr"
_REMOTE_HOST_DIR: Final[str] = "/mngr"
_PREAUTH_TOKEN_LENGTH: Final[int] = 64

OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]
OnReverseTunnelEstablishedCallback = Callable[["ReverseTunnelEstablishedInfo"], None]
OnProviderErrorCallback = Callable[[str, str, str], None]


class ReverseTunnelEstablishedInfo(FrozenModel):
    """Decoded ``forward.reverse_tunnel_established`` payload from the plugin."""

    agent_id: AgentId = Field(description="Agent the tunnel was set up for")
    remote_port: int = Field(description="Port on the remote sshd that was bound")
    local_port: int = Field(description="Local port the tunnel forwards to")
    ssh_host: str = Field(description="SSH host the reverse tunnel runs over")
    ssh_port: int = Field(description="SSH port on ssh_host")


class ForwardSubprocessConfig(FrozenModel):
    """Args for the ``mngr forward`` subprocess that ``minds run`` spawns.

    Note: the preauth cookie is *not* a configurable field. It is freshly
    generated inside ``start_mngr_forward`` (so each run has a fresh
    secret) and returned to the caller as the second element of the
    tuple. Callers hand it to the Electron shell, which pre-sets
    ``mngr_forward_session=<value>`` on ``localhost:<port>``.
    """

    port: int = Field(description="Plugin bind port (e.g. 8421)")
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
    notification_dispatcher: NotificationDispatcher | None = Field(
        default=None,
        description="Dispatcher used to surface plugin-exit failures to the user",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _discovered_agents: dict[str, DiscoveredAgent] = PrivateAttr(default_factory=dict)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _on_reverse_tunnel_established_callbacks: list[OnReverseTunnelEstablishedCallback] = PrivateAttr(
        default_factory=list
    )
    _on_provider_error_callbacks: list[OnProviderErrorCallback] = PrivateAttr(default_factory=list)
    _process: subprocess.Popen[bytes] | None = PrivateAttr(default=None)
    _has_notified_exit: bool = PrivateAttr(default=False)
    _intentional_shutdown: bool = PrivateAttr(default=False)

    # -- Public callback registration -------------------------------------

    def add_on_agent_discovered_callback(self, callback: OnAgentDiscoveredCallback) -> None:
        """Register a callback fired for every observe-stream agent discovery."""
        with self._lock:
            self._on_agent_discovered_callbacks.append(callback)

    def add_on_agent_destroyed_callback(self, callback: OnAgentDestroyedCallback) -> None:
        """Register a callback fired for every observe-stream agent destruction."""
        with self._lock:
            self._on_agent_destroyed_callbacks.append(callback)

    def add_on_reverse_tunnel_established_callback(self, callback: OnReverseTunnelEstablishedCallback) -> None:
        """Register a callback fired for each ``reverse_tunnel_established`` envelope."""
        with self._lock:
            self._on_reverse_tunnel_established_callbacks.append(callback)

    def add_on_provider_error_callback(self, callback: OnProviderErrorCallback) -> None:
        """Register a callback fired for each ``DiscoveryErrorEvent`` attributable to a provider.

        The callback receives ``(provider_name, error_type, error_message)``.
        Used by minds to auto-disable an imbue_cloud account whose session has
        been revoked server-side. Callbacks should be fast and non-blocking;
        they run on the envelope-stream reader thread, so any work that needs
        to terminate other subprocesses (e.g. bouncing observe) must dispatch
        onto a worker thread itself.
        """
        with self._lock:
            self._on_provider_error_callbacks.append(callback)

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
            raise RuntimeError("EnvelopeStreamConsumer.attach already called")
        self._process = process

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the reader / lifecycle threads for the attached subprocess.

        Must be called after ``attach()`` and after any callbacks that
        need to see the very first envelope have been registered.
        """
        if self._process is None:
            raise RuntimeError("EnvelopeStreamConsumer.start called before attach")
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
            target=self._wait_and_notify_on_exit,
            name="mngr-forward-lifecycle-watcher",
            daemon=True,
            is_checked=False,
        )

    def bounce_observe(self) -> None:
        """Send ``SIGHUP`` to the plugin so its observe child is bounced.

        Used after writing a new ``[providers.imbue_cloud_<slug>]`` block so
        the freshly-registered provider becomes visible. Per-agent event
        subprocesses, SSH tunnels, and the FastAPI app on the plugin side
        stay alive (the plugin's SIGHUP handler only restarts ``mngr observe``).

        No-op if the plugin process is no longer running.
        """
        process = self._process
        if process is None or process.poll() is not None:
            logger.debug("bounce_observe: plugin not running; skipping")
            return
        try:
            os.kill(process.pid, signal.SIGHUP)
        except OSError as e:
            logger.warning("bounce_observe: failed to send SIGHUP to {}: {}", process.pid, e)

    def terminate(self) -> None:
        """Stop the plugin subprocess (SIGTERM, then SIGKILL on timeout).

        Sets ``_intentional_shutdown`` *before* signalling the subprocess
        so the lifecycle watcher (``_wait_and_notify_on_exit``) does not
        surface the resulting non-zero exit code as a CRITICAL "Forwarding
        subprocess died" notification.
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

    def _wait_and_notify_on_exit(self) -> None:
        process = self._process
        if process is None:
            return
        exit_code = process.wait()
        # If minds asked the subprocess to stop (lifespan shutdown), the
        # non-zero exit code is the expected SIGTERM/SIGKILL signal, not a
        # crash. Surfacing it as a CRITICAL notification on every clean
        # shutdown trains the user to ignore the notification entirely,
        # which defeats its purpose for the crash-on-its-own case.
        if self._intentional_shutdown:
            logger.debug("mngr forward exited with code {} after intentional shutdown", exit_code)
            return
        if exit_code != 0 and not self._has_notified_exit:
            self._has_notified_exit = True
            logger.error("mngr forward exited with code {}", exit_code)
            if self.notification_dispatcher is not None:
                self.notification_dispatcher.dispatch(
                    NotificationRequest(
                        title="Forwarding subprocess died",
                        message=(
                            f"`mngr forward` exited with code {exit_code}. The minds desktop client "
                            "is no longer forwarding agent traffic; restart minds to recover."
                        ),
                        urgency=NotificationUrgency.CRITICAL,
                    ),
                    agent_display_name="Minds",
                )

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
        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
        elif isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, AgentDestroyedEvent):
            self._handle_agent_destroyed(event.agent_id)
        elif isinstance(event, HostDestroyedEvent):
            for aid in event.agent_ids:
                self._handle_agent_destroyed(aid)
            with self._lock:
                self._ssh_by_host_id.pop(str(event.host_id), None)
        elif isinstance(event, DiscoveryErrorEvent):
            logger.warning(
                "Discovery error from {}: {} ({})", event.source_name, event.error_message, event.error_type
            )
            if event.provider_name is not None:
                self._fire_provider_error(event.provider_name, event.error_type, event.error_message)
        else:
            # parse_discovery_event_line returns the union we already
            # exhaustively enumerated above; an unknown event type means
            # mngr added a new discovery type the plugin still passes
            # through. Log once at trace-level so it's visible without
            # being noisy.
            logger.trace("Ignoring unknown discovery event: {}", type(event).__name__)

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        agent_ids: list[AgentId] = []
        agent_host_map: dict[str, str] = {}
        kept: dict[str, DiscoveredAgent] = {}
        for agent in event.agents:
            agent_ids.append(agent.agent_id)
            agent_host_map[str(agent.agent_id)] = str(agent.host_id)
            kept[str(agent.agent_id)] = agent
        with self._lock:
            previously_known = set(self._discovered_agents.keys())
            self._discovered_agents = kept
            self._agent_host_map = agent_host_map
            ssh_info_by_agent = {
                aid: self._ssh_by_host_id[hid] for aid, hid in agent_host_map.items() if hid in self._ssh_by_host_id
            }
            removed = previously_known - set(kept.keys())
        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=tuple(agent_ids),
                discovered_agents=event.agents,
                ssh_info_by_agent_id=ssh_info_by_agent,
            )
        )
        for aid_str in removed:
            self._fire_destroyed(AgentId(aid_str))
        for agent in event.agents:
            ssh_info = ssh_info_by_agent.get(str(agent.agent_id))
            self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user, host=event.ssh.host, port=event.ssh.port, key_path=event.ssh.key_path
        )
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
            agents_on_host = [AgentId(aid) for aid, hid in self._agent_host_map.items() if hid == host_id_str]
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            ssh_info_by_agent = {
                aid: self._ssh_by_host_id[hid]
                for aid, hid in self._agent_host_map.items()
                if hid in self._ssh_by_host_id
            }
            discovered = tuple(self._discovered_agents.values())
        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=agent_ids, discovered_agents=discovered, ssh_info_by_agent_id=ssh_info_by_agent
            )
        )
        for agent_id in agents_on_host:
            self._fire_discovered(agent_id, ssh_info, self._provider_name_for_agent(agent_id))

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        agent = event.agent
        aid_str = str(agent.agent_id)
        with self._lock:
            self._agent_host_map[aid_str] = str(agent.host_id)
            self._discovered_agents[aid_str] = agent
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            ssh_info_by_agent = {
                aid: self._ssh_by_host_id[hid]
                for aid, hid in self._agent_host_map.items()
                if hid in self._ssh_by_host_id
            }
            discovered = tuple(self._discovered_agents.values())
            ssh_info = self._ssh_by_host_id.get(str(agent.host_id))
        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=agent_ids, discovered_agents=discovered, ssh_info_by_agent_id=ssh_info_by_agent
            )
        )
        self._fire_discovered(agent.agent_id, ssh_info, str(agent.provider_name))

    def _handle_agent_destroyed(self, agent_id: AgentId) -> None:
        aid_str = str(agent_id)
        with self._lock:
            self._discovered_agents.pop(aid_str, None)
            self._agent_host_map.pop(aid_str, None)
            self._services_by_agent.pop(aid_str, None)
            agent_ids = tuple(AgentId(aid) for aid in self._agent_host_map)
            ssh_info_by_agent = {
                aid: self._ssh_by_host_id[hid]
                for aid, hid in self._agent_host_map.items()
                if hid in self._ssh_by_host_id
            }
            discovered = tuple(self._discovered_agents.values())
        self.resolver.update_agents(
            ParsedAgentsResult(
                agent_ids=agent_ids, discovered_agents=discovered, ssh_info_by_agent_id=ssh_info_by_agent
            )
        )
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

    def _fire_provider_error(self, provider_name: str, error_type: str, error_message: str) -> None:
        with self._lock:
            callbacks = list(self._on_provider_error_callbacks)
        for callback in callbacks:
            try:
                callback(provider_name, error_type, error_message)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_provider_error callback failed for {}: {}", provider_name, e)

    # -- Per-agent event lines (services / requests / refresh) ------------

    def _handle_event_payload(self, agent_id: AgentId, payload: dict[str, Any]) -> None:
        source = payload.get("source", "")
        aid_str = str(agent_id)
        if source == REQUESTS_EVENT_SOURCE_NAME:
            raw_line = json.dumps(payload, separators=(",", ":"))
            self.resolver.fire_on_request(aid_str, raw_line)
            return
        if source == REFRESH_EVENT_SOURCE_NAME:
            raw_line = json.dumps(payload, separators=(",", ":"))
            self.resolver.fire_on_refresh(aid_str, raw_line)
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

    def _handle_forward_payload(self, payload: dict[str, Any]) -> None:
        payload_type = payload.get("type")
        if payload_type == "reverse_tunnel_established":
            try:
                info = ReverseTunnelEstablishedInfo(
                    agent_id=AgentId(str(payload["agent_id"])),
                    remote_port=int(payload["remote_port"]),
                    local_port=int(payload["local_port"]),
                    ssh_host=str(payload["ssh_host"]),
                    ssh_port=int(payload["ssh_port"]),
                )
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Could not parse reverse_tunnel_established payload: {}", e)
                return
            with self._lock:
                callbacks = list(self._on_reverse_tunnel_established_callbacks)
            for callback in callbacks:
                try:
                    callback(info)
                except (OSError, RuntimeError, paramiko.SSHException) as e:
                    logger.warning("reverse_tunnel_established callback failed for {}: {}", info.agent_id, e)
        elif payload_type in ("login_url", "listening"):
            logger.debug("Forward stream payload {}: {}", payload_type, payload)
        else:
            logger.trace("Unknown forward payload type {!r}", payload_type)


# -- Helpers run from the consumer's callbacks ------------------------------


class MindsApiUrlWriter(MutableModel):
    """``on_reverse_tunnel_established`` callback that writes ``minds_api_url`` on remote agents.

    Opens a fresh paramiko connection per event (using SSH info from the
    surviving resolver) and overwrites ``<state_dir>/minds_api_url`` with
    ``http://127.0.0.1:<remote_port>``. The write is unconditional — if the
    plugin re-emits the event with a different remote port (sshd reassigned
    the dynamic-bind), we just overwrite.
    """

    resolver: MngrCliBackendResolver = Field(frozen=True, description="Source of cached SSH info")

    def __call__(self, info: ReverseTunnelEstablishedInfo) -> None:
        ssh_info = self.resolver.get_ssh_info(info.agent_id)
        if ssh_info is None:
            logger.debug(
                "MindsApiUrlWriter: no ssh_info for {}; skipping minds_api_url write",
                info.agent_id,
            )
            return
        url = f"http://127.0.0.1:{info.remote_port}"
        agent_state_dir = f"{_REMOTE_HOST_DIR}/agents/{info.agent_id}"
        try:
            client = open_ssh_client(ssh_info)
        except (paramiko.SSHException, OSError) as e:
            logger.warning("MindsApiUrlWriter: SSH connect failed for {}: {}", info.agent_id, e)
            return
        try:
            quoted_dir = shlex.quote(agent_state_dir)
            quoted_url = shlex.quote(url)
            command = f"mkdir -p {quoted_dir} && printf '%s' {quoted_url} > {quoted_dir}/minds_api_url"
            try:
                _stdin, stdout, _stderr = client.exec_command(command, timeout=10.0)
                _stdin.close()
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    logger.warning(
                        "MindsApiUrlWriter: remote write failed for {}: exit={}",
                        info.agent_id,
                        exit_status,
                    )
            except (paramiko.SSHException, OSError) as e:
                logger.warning("MindsApiUrlWriter: write failed for {}: {}", info.agent_id, e)
        finally:
            try:
                client.close()
            except (paramiko.SSHException, OSError) as e:
                logger.trace("Error closing SSH client after url write: {}", e)


class LocalAgentDiscoveryHandler(MutableModel):
    """``on_agent_discovered`` callback covering minds-specific local-agent setup.

    For local agents (``ssh_info is None``), writes ``minds_api_url`` to
    ``<MNGR_HOST_DIR>/agents/<agent-id>/minds_api_url`` so the workspace
    server can talk back to minds without a tunnel.

    Note: the previous Cloudflare-tunnel-token re-injection step is gone —
    the agent's container persists the token itself, and any rebuild path
    re-triggers ``_run_tunnel_setup`` from agent-create instead.
    """

    minds_api_port: int = Field(frozen=True, description="Port the minds-side bare-origin server binds")
    mngr_host_dir: Path = Field(
        default_factory=lambda: _DEFAULT_MNGR_HOST_DIR,
        description="MNGR_HOST_DIR for local-agent state-dir discovery",
    )

    def __call__(
        self,
        agent_id: AgentId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        del provider_name
        if ssh_info is None:
            self._write_local_minds_api_url(agent_id)

    def _write_local_minds_api_url(self, agent_id: AgentId) -> None:
        state_dir = self.mngr_host_dir / "agents" / str(agent_id)
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            url_file = state_dir / "minds_api_url"
            url_file.write_text(f"http://127.0.0.1:{self.minds_api_port}")
        except OSError as e:
            logger.warning("Could not write minds_api_url for local agent {}: {}", agent_id, e)


# -- start_mngr_forward ----------------------------------------------------


def start_mngr_forward(
    config: ForwardSubprocessConfig,
    resolver: MngrCliBackendResolver,
    notification_dispatcher: NotificationDispatcher | None = None,
) -> tuple[EnvelopeStreamConsumer, str]:
    """Spawn the ``mngr forward`` subprocess and attach an envelope consumer.

    Returns ``(consumer, preauth_cookie_value)``. The reader threads are
    *not* started yet -- the caller MUST:

    1. register its on_agent_discovered / on_agent_destroyed /
       on_reverse_tunnel_established handlers on the consumer;
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
    binary = _resolve_mngr_binary(config.mngr_binary)
    preauth_cookie = secrets.token_urlsafe(_PREAUTH_TOKEN_LENGTH)
    command: list[str] = [
        binary,
        "forward",
        "--host",
        "127.0.0.1",
        "--port",
        str(config.port),
        "--service",
        config.service,
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
    consumer = EnvelopeStreamConsumer(
        resolver=resolver,
        notification_dispatcher=notification_dispatcher,
    )
    consumer.attach(process)
    return consumer, preauth_cookie


def _resolve_mngr_binary(candidate: str) -> str:
    """Resolve the mngr binary, falling back to PATH lookup if the candidate is just 'mngr'."""
    if "/" in candidate:
        return candidate
    resolved = shutil.which(candidate)
    if resolved is None:
        # Best-effort: trust the bare name. Popen will raise if it's missing.
        return candidate
    return resolved


def _redact_secrets(command: list[str]) -> list[str]:
    """Return a copy of ``command`` with secret-bearing argument values masked.

    Used only for logging. The actual ``Popen`` call uses the unredacted
    list so the plugin still receives the real values. Today we redact the
    ``--preauth-cookie`` value (a freshly-minted shared secret between
    minds, the plugin, and the Electron shell); future secret-bearing
    flags can be added to ``_SECRET_BEARING_FLAGS``.
    """
    redacted = list(command)
    for flag in _SECRET_BEARING_FLAGS:
        try:
            idx = redacted.index(flag)
        except ValueError:
            continue
        if idx + 1 < len(redacted):
            redacted[idx + 1] = "***"
    return redacted


_SECRET_BEARING_FLAGS: Final[tuple[str, ...]] = ("--preauth-cookie",)
