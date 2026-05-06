"""Spawns and manages ``mngr observe`` + per-agent ``mngr event`` subprocesses.

Adapted from ``minds.desktop_client.backend_resolver.MngrStreamManager``,
slimmed to the parts the plugin needs:

- One ``mngr observe --discovery-only --quiet`` subprocess produces discovery
  events. Lines pass through to the envelope writer's ``observe`` stream and
  drive the ``ForwardResolver``'s known-agent set + per-host SSH info.
- One ``mngr event <id> services requests refresh --follow --quiet`` per
  filter-matching agent produces service-registration / request / refresh
  events. Lines pass through to the envelope writer's ``event`` stream and
  drive the resolver's per-agent service map.
- ``bounce_observe()`` terminates only the observe subprocess and respawns it
  with the same args; per-agent event subprocesses, registered callbacks, and
  resolver state survive.

CEL filters from ``--agent-include`` / ``--agent-exclude`` /
``--event-include`` / ``--event-exclude`` are applied client-side after each
line is parsed.
"""

import json
import threading
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import MNGR_BINARY
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_SERVICES_SOURCE = "services"
_REQUESTS_SOURCE = "requests"
_REFRESH_SOURCE = "refresh"


OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]


class ForwardStreamManager(MutableModel):
    """Manage the plugin's two stream-style mngr subprocesses."""

    resolver: ForwardResolver = Field(frozen=True, description="Resolver to update")
    envelope_writer: EnvelopeWriter = Field(frozen=True, description="Where parsed lines fan out to")
    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to the mngr binary")
    agent_include: tuple[str, ...] = Field(
        default=(),
        frozen=True,
        description="CEL include filters for which agents the plugin tracks (default: empty = all)",
    )
    agent_exclude: tuple[str, ...] = Field(
        default=(),
        frozen=True,
        description="CEL exclude filters for which agents the plugin tracks",
    )
    event_sources: tuple[str, ...] = Field(
        default=(_SERVICES_SOURCE, _REQUESTS_SOURCE, _REFRESH_SOURCE),
        frozen=True,
        description="Source streams to follow per-agent (passed to ``mngr event``)",
    )
    event_include: tuple[str, ...] = Field(
        default=(),
        frozen=True,
        description=(
            "CEL include filters for which event source streams are followed. "
            "Evaluated against context ``{'event': {'source': <source_name>}}``. "
            "Default: empty -- include every source in ``event_sources``."
        ),
    )
    event_exclude: tuple[str, ...] = Field(
        default=(),
        frozen=True,
        description="CEL exclude filters for which event source streams are followed.",
    )

    _cg: ConcurrencyGroup = PrivateAttr(default_factory=lambda: ConcurrencyGroup(name="mngr-forward-stream"))
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _agent_host_map: dict[str, str] = PrivateAttr(default_factory=dict)
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _discovered_agents: dict[str, DiscoveredAgent] = PrivateAttr(default_factory=dict)
    _observe_process: RunningProcess | None = PrivateAttr(default=None)
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _events_services: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _compiled_includes: list[Any] = PrivateAttr(default_factory=list)
    _compiled_excludes: list[Any] = PrivateAttr(default_factory=list)
    _compiled_event_includes: list[Any] = PrivateAttr(default_factory=list)
    _compiled_event_excludes: list[Any] = PrivateAttr(default_factory=list)
    _filtered_event_sources: tuple[str, ...] = PrivateAttr(default=())

    def model_post_init(self, __context: Any) -> None:
        compiled_includes, compiled_excludes = compile_cel_filters(
            list(self.agent_include),
            list(self.agent_exclude),
        )
        self._compiled_includes = compiled_includes
        self._compiled_excludes = compiled_excludes
        compiled_event_includes, compiled_event_excludes = compile_cel_filters(
            list(self.event_include),
            list(self.event_exclude),
        )
        self._compiled_event_includes = compiled_event_includes
        self._compiled_event_excludes = compiled_event_excludes
        # Resolve the per-source filter once at startup: the source list is
        # static (just the strings in ``event_sources``), so we don't need to
        # re-evaluate the CEL programs per spawn.
        self._filtered_event_sources = self._resolve_event_sources()

    def _resolve_event_sources(self) -> tuple[str, ...]:
        """Apply ``--event-include`` / ``--event-exclude`` to ``event_sources``.

        Called once from ``model_post_init``; the result is cached on
        ``_filtered_event_sources`` and read by ``_start_events_stream`` for
        every per-agent spawn.
        """
        if not self._compiled_event_includes and not self._compiled_event_excludes:
            return self.event_sources
        kept: list[str] = []
        for source in self.event_sources:
            context = {"event": {"source": source}}
            if apply_cel_filters_to_context(
                context=context,
                include_filters=self._compiled_event_includes,
                exclude_filters=self._compiled_event_excludes,
                error_context_description=f"event source {source}",
            ):
                kept.append(source)
        return tuple(kept)

    # -- callback registration --------------------------------------------

    def add_on_agent_discovered_callback(self, callback: OnAgentDiscoveredCallback) -> None:
        """Register a callback fired for every agent discovered via the observe stream."""
        self._on_agent_discovered_callbacks.append(callback)

    def add_on_agent_destroyed_callback(self, callback: OnAgentDestroyedCallback) -> None:
        """Register a callback fired for every agent destruction from the observe stream."""
        self._on_agent_destroyed_callbacks.append(callback)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the observe subprocess. Per-agent event subprocesses are started lazily."""
        self._cg.__enter__()
        self._observe_process = self._spawn_observe()

    def stop(self) -> None:
        """Terminate every managed subprocess and exit the ConcurrencyGroup."""
        for process in self._all_managed_processes():
            try:
                process.terminate()
            except (OSError, RuntimeError) as e:
                logger.trace("Error terminating subprocess: {}", e)
        self._cg.__exit__(None, None, None)

    def bounce_observe(self) -> None:
        """Terminate and respawn the observe subprocess only.

        Per-agent event subprocesses, registered callbacks, and resolver
        state are all left intact. Used by ``SIGHUP`` to make
        ``settings.toml`` provider changes take effect without restarting
        the whole plugin.
        """
        if self._observe_process is None:
            logger.debug("bounce_observe: no observe process running; skipping")
            return
        logger.info("Bouncing mngr observe subprocess")
        try:
            self._observe_process.terminate()
        except (OSError, RuntimeError) as e:
            logger.warning("Failed to terminate observe process during bounce: {}", e)
        try:
            self._observe_process = self._spawn_observe()
        except InvalidConcurrencyGroupStateError:
            logger.debug("bounce_observe: concurrency group no longer active; skipping respawn")
            self._observe_process = None

    # -- internals ---------------------------------------------------------

    def _spawn_observe(self) -> RunningProcess:
        return self._cg.run_process_in_background(
            command=[self.mngr_binary, "observe", "--discovery-only", "--quiet"],
            on_output=self._on_observe_output,
            cwd=Path.home(),
        )

    def _all_managed_processes(self) -> list[RunningProcess]:
        result: list[RunningProcess] = []
        if self._observe_process is not None:
            result.append(self._observe_process)
        result.extend(self._events_processes.values())
        return result

    def _on_observe_output(self, line: str, is_stdout: bool) -> None:
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr observe stderr: {}", stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        # Pass through to the envelope writer regardless of whether we
        # successfully parse the event below — consumers may want to
        # introspect it themselves.
        self.envelope_writer.emit_observe(stripped)
        try:
            event = parse_discovery_event_line(stripped)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("Failed to parse discovery line {!r}: {}", stripped[:200], e)
            return
        if event is None:
            return
        self._handle_discovery_event(event)

    def _handle_discovery_event(self, event: Any) -> None:
        if isinstance(event, FullDiscoverySnapshotEvent):
            self._handle_full_snapshot(event)
        elif isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, AgentDestroyedEvent):
            self._handle_agent_destroyed(event)
        elif isinstance(event, HostDestroyedEvent):
            self._handle_host_destroyed(event)
        elif isinstance(event, DiscoveryErrorEvent):
            logger.warning(
                "Discovery error from {}: {} ({})",
                event.source_name,
                event.error_message,
                event.error_type,
            )
        else:
            logger.trace("Ignoring discovery event of type {}", type(event).__name__)

    def _agent_passes_filter(self, agent: DiscoveredAgent) -> bool:
        if not self._compiled_includes and not self._compiled_excludes:
            return True
        context = {
            "agent": {
                "id": str(agent.agent_id),
                "name": str(agent.agent_name),
                "host_id": str(agent.host_id),
                "provider_name": str(agent.provider_name),
                "labels": dict(agent.labels),
            }
        }
        return apply_cel_filters_to_context(
            context=context,
            include_filters=self._compiled_includes,
            exclude_filters=self._compiled_excludes,
            error_context_description=f"agent {agent.agent_id}",
        )

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        kept_ids: list[AgentId] = []
        kept_agents: dict[str, DiscoveredAgent] = {}
        agent_host_map: dict[str, str] = {}
        for agent in event.agents:
            if not self._agent_passes_filter(agent):
                continue
            kept_ids.append(agent.agent_id)
            kept_agents[str(agent.agent_id)] = agent
            agent_host_map[str(agent.agent_id)] = str(agent.host_id)

        with self._lock:
            previously_known = set(self._discovered_agents.keys())
            self._discovered_agents = kept_agents
            self._agent_host_map = agent_host_map
            new_known = set(agent_host_map.keys())
            removed = previously_known - new_known

        self.resolver.update_known_agents(tuple(kept_ids))

        for aid_str in removed:
            self._stop_events_stream(AgentId(aid_str))
            for callback in self._on_agent_destroyed_callbacks:
                self._safely_call(callback, AgentId(aid_str), name="on_agent_destroyed")

        for agent in kept_agents.values():
            ssh_info = self._ssh_for_agent(agent.agent_id)
            if ssh_info is not None:
                self.resolver.update_ssh_info(agent.agent_id, ssh_info)
            self._start_events_stream(agent.agent_id)
            for callback in self._on_agent_discovered_callbacks:
                self._safely_call(
                    callback,
                    agent.agent_id,
                    ssh_info,
                    str(agent.provider_name),
                    name="on_agent_discovered",
                )

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user,
            host=event.ssh.host,
            port=event.ssh.port,
            key_path=event.ssh.key_path,
        )
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
            agents_on_host = [AgentId(aid) for aid, hid in self._agent_host_map.items() if hid == host_id_str]

        for agent_id in agents_on_host:
            self.resolver.update_ssh_info(agent_id, ssh_info)
            for callback in self._on_agent_discovered_callbacks:
                self._safely_call(
                    callback,
                    agent_id,
                    ssh_info,
                    self._provider_name_for_agent(agent_id),
                    name="on_agent_discovered (ssh-info-late)",
                )

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        agent = event.agent
        if not self._agent_passes_filter(agent):
            return
        aid_str = str(agent.agent_id)
        with self._lock:
            self._discovered_agents[aid_str] = agent
            self._agent_host_map[aid_str] = str(agent.host_id)
        self.resolver.add_known_agent(agent.agent_id)
        ssh_info = self._ssh_for_agent(agent.agent_id)
        if ssh_info is not None:
            self.resolver.update_ssh_info(agent.agent_id, ssh_info)
        self._start_events_stream(agent.agent_id)
        for callback in self._on_agent_discovered_callbacks:
            self._safely_call(
                callback,
                agent.agent_id,
                ssh_info,
                str(agent.provider_name),
                name="on_agent_discovered",
            )

    def _handle_agent_destroyed(self, event: AgentDestroyedEvent) -> None:
        self._destroy_agent(event.agent_id)

    def _handle_host_destroyed(self, event: HostDestroyedEvent) -> None:
        for agent_id in event.agent_ids:
            self._destroy_agent(agent_id)
        with self._lock:
            self._ssh_by_host_id.pop(str(event.host_id), None)

    def _destroy_agent(self, agent_id: AgentId) -> None:
        aid_str = str(agent_id)
        with self._lock:
            self._discovered_agents.pop(aid_str, None)
            self._agent_host_map.pop(aid_str, None)
            self._events_services.pop(aid_str, None)
        self.resolver.remove_known_agent(agent_id)
        self._stop_events_stream(agent_id)
        for callback in self._on_agent_destroyed_callbacks:
            self._safely_call(callback, agent_id, name="on_agent_destroyed")

    def _ssh_for_agent(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        with self._lock:
            host_id = self._agent_host_map.get(str(agent_id))
            if host_id is None:
                return None
            return self._ssh_by_host_id.get(host_id)

    def _provider_name_for_agent(self, agent_id: AgentId) -> str:
        with self._lock:
            agent = self._discovered_agents.get(str(agent_id))
        if agent is None:
            return "unknown"
        return str(agent.provider_name)

    # -- per-agent events streams -----------------------------------------

    def _start_events_stream(self, agent_id: AgentId) -> None:
        if self._cg.is_shutting_down():
            return
        if not self._filtered_event_sources:
            # Either ``event_sources`` was empty to begin with, or every
            # source was filtered out by ``--event-include`` / ``--event-exclude``.
            return
        aid_str = str(agent_id)
        with self._lock:
            if aid_str in self._events_processes:
                return
            self._events_services[aid_str] = {}
        sources: Sequence[str] = self._filtered_event_sources
        try:
            process = self._cg.run_process_in_background(
                command=[
                    self.mngr_binary,
                    "event",
                    aid_str,
                    *sources,
                    "--follow",
                    "--quiet",
                ],
                on_output=lambda line, is_stdout, _aid=agent_id: self._on_event_output(line, is_stdout, _aid),
                cwd=Path.home(),
            )
            with self._lock:
                self._events_processes[aid_str] = process
        except InvalidConcurrencyGroupStateError:
            logger.debug("Skipping events stream for {} -- concurrency group inactive", agent_id)

    def _stop_events_stream(self, agent_id: AgentId) -> None:
        aid_str = str(agent_id)
        with self._lock:
            process = self._events_processes.pop(aid_str, None)
        if process is None:
            return
        try:
            process.terminate()
        except (OSError, RuntimeError) as e:
            logger.trace("Error terminating events stream for {}: {}", agent_id, e)

    def _on_event_output(self, line: str, is_stdout: bool, agent_id: AgentId) -> None:
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr event stderr for {}: {}", agent_id, stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        self.envelope_writer.emit_event(agent_id, stripped)

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse event line for {}: {}", agent_id, e)
            return
        if not isinstance(raw, dict):
            return
        source = raw.get("source")
        if source != _SERVICES_SOURCE:
            # Requests / refresh events are passed through to consumers via
            # the envelope; the plugin doesn't consume them itself.
            return

        event_type = raw.get("type", "service_registered")
        service = raw.get("service")
        if not isinstance(service, str) or not service:
            return

        aid_str = str(agent_id)
        with self._lock:
            services = self._events_services.setdefault(aid_str, {})
            if event_type == "service_deregistered":
                services.pop(service, None)
            else:
                url = raw.get("url")
                if isinstance(url, str) and url:
                    services[service] = url
            services_snapshot = dict(services)
        self.resolver.update_services(agent_id, services_snapshot)

    @staticmethod
    def _safely_call(callback: Callable[..., None], *args: Any, name: str) -> None:
        try:
            callback(*args)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("{} callback failed: {}", name, e)
