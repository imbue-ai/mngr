"""Spawns and manages ``mngr observe`` + per-agent ``mngr event`` subprocesses.

Adapted from ``minds.desktop_client.backend_resolver.MngrStreamManager``,
slimmed to the parts the plugin needs:

- Discovery events come from one of two sources: a ``mngr observe
  --discovery-only --quiet`` subprocess (default), or, when
  ``discovery_events_path`` is set (``mngr forward --observe-via-file``), an
  in-process tail of a shared discovery events file written by another
  observer. Either way lines drive the envelope writer's ``observe`` stream and
  the ``ForwardResolver``'s known-agent set + per-host SSH info.
- One ``mngr event <agent_id>@<host_id> services requests --follow --quiet``
  per filter-matching agent instance produces service-registration / request
  events (host-scoped addressing, since an agent id is only unique per host).
  Lines pass through to the envelope writer's ``event`` stream and drive
  the resolver's per-agent service map.
- ``bounce_observe()`` terminates only the observe subprocess and respawns it
  with the same args; per-agent event subprocesses, registered callbacks, and
  resolver state survive.

CEL filters from ``--agent-include`` / ``--agent-exclude`` /
``--event-include`` / ``--event-exclude`` are applied client-side after each
line is parsed.
"""

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_aggregator import AggregatorDelta
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoverySchemaMismatchWarner
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import tail_discovery_events_file
from imbue.mngr.api.discovery_log_suppression import DiscoveryErrorLogSuppressor
from imbue.mngr.errors import EXIT_CODE_TARGET_NOT_FOUND
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.utils.cel_utils import apply_cel_filters_to_context
from imbue.mngr.utils.cel_utils import compile_cel_filters
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import MNGR_BINARY
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_SERVICES_SOURCE = "services"
_REQUESTS_SOURCE = "requests"

# Respawn pacing for per-agent events streams: exponential backoff between
# respawns of a stream that keeps dying, reset once a stream survives long
# enough to count as healthy (its later death is a fresh incident, not a
# continuation of a crash loop).
_EVENTS_RESPAWN_INITIAL_BACKOFF_SECONDS: Final[float] = 2.0
_EVENTS_RESPAWN_MAX_BACKOFF_SECONDS: Final[float] = 60.0
_EVENTS_STREAM_HEALTHY_AGE_SECONDS: Final[float] = 60.0
# A child whose stderr says its target agent/host does not exist cannot succeed
# until discovery changes its mind, so it retries far less often than the
# crash-loop ladder's cap. Still finite: the child's view can be transiently
# stale (e.g. an agent listing hiccup during a host restart), so never respawning
# again would strand a live agent's stream -- the exact wedge this plugin's
# respawn machinery exists to prevent.
_EVENTS_GONE_TARGET_RESPAWN_BACKOFF_SECONDS: Final[float] = 900.0
# How many of a child's most recent stderr lines are kept for its exit log line.
_EVENTS_STDERR_TAIL_MAX_LINES: Final[int] = 5
# How many gone-target exits, with no healthy run in between, before the long
# backoff applies. A stream is only ever started for an agent this forward's own
# aggregator currently tracks, so a gone-target exit means the child's discovery
# view and ours disagree -- and one disagreement is not worth 15 minutes of
# silence for an agent that may well be alive.
_EVENTS_GONE_TARGET_STRIKES_REQUIRED: Final[int] = 2


OnAgentDiscoveredCallback = Callable[[AgentId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]


class ForwardStreamManager(MutableModel):
    """Manage the plugin's two stream-style mngr subprocesses."""

    resolver: ForwardResolver = Field(frozen=True, description="Resolver to update")
    envelope_writer: EnvelopeWriter = Field(frozen=True, description="Where parsed lines fan out to")
    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path to the mngr binary")
    discovery_events_path: Path | None = Field(
        default=None,
        frozen=True,
        description=(
            "If set (``--observe-via-file``), discovery is driven by tailing this discovery events file "
            "in-process instead of spawning a ``mngr observe`` subprocess. Used when another process "
            "(e.g. ``mngr latchkey forward``) is the sole discovery observer writing this shared log."
        ),
    )
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
        default=(_SERVICES_SOURCE, _REQUESTS_SOURCE),
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
    _aggregator: DiscoveryStateAggregator = PrivateAttr(default_factory=DiscoveryStateAggregator)
    # Deduplicates provider-level discovery-error warnings: a provider wedged on
    # the same failure (e.g. missing credentials) logs once per process, not once
    # per poll cycle. Clean snapshots feed it to re-arm on recovery.
    _error_log_suppressor: DiscoveryErrorLogSuppressor = PrivateAttr(default_factory=DiscoveryErrorLogSuppressor)
    # Deduplicates warnings for discovery lines that do not match this version's
    # schema (the shared log / observe echo can carry lines written by other
    # mngr versions).
    _discovery_schema_warner: DiscoverySchemaMismatchWarner = PrivateAttr(
        default_factory=lambda: DiscoverySchemaMismatchWarner(source_description="mngr forward discovery stream")
    )
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _observe_process: RunningProcess | None = PrivateAttr(default=None)
    _tail_stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    # The per-agent maps below are keyed by the agent *instance* key string
    # (``<agent_id>@<host_id>``): agent ids are unique per host, not globally,
    # so each instance owns its own events stream, services, and pacing.
    _events_processes: dict[str, RunningProcess] = PrivateAttr(default_factory=dict)
    _events_services: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Per-instance service name -> origin label, from the same services stream.
    _events_labels: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Per-instance respawn pacing for the events streams. A stream that dies
    # instantly (unreachable host) must not be respawned at the discovery
    # snapshot cadence: against an SSH server with per-source penalties, a
    # tight reconnect loop turns one transient failure into a permanent
    # lockout of every connection from this machine.
    _events_respawn_backoff_by_instance: dict[str, float] = PrivateAttr(default_factory=dict)
    _events_next_respawn_at_by_instance: dict[str, float] = PrivateAttr(default_factory=dict)
    _events_spawned_at_by_instance: dict[str, float] = PrivateAttr(default_factory=dict)
    # The most recent stderr lines per live events child, surfaced in its exit
    # log line: the child's stderr is otherwise logged at debug only, which made
    # every returncode=1 exit undiagnosable from a default-level log bundle.
    _events_stderr_tail_by_instance: dict[str, deque[str]] = PrivateAttr(default_factory=dict)
    # Consecutive gone-target exits per instance; reset by any other exit reason.
    _events_gone_target_strikes_by_instance: dict[str, int] = PrivateAttr(default_factory=dict)
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
        """Start discovery (observe subprocess, or file tail). Per-agent event subprocesses start lazily."""
        self._cg.__enter__()
        if self.discovery_events_path is not None:
            self._start_tail_discovery()
        else:
            self._observe_process = self._spawn_observe()

    def stop(self) -> None:
        """Terminate every managed subprocess (and stop the file tail) and exit the ConcurrencyGroup."""
        self._tail_stop_event.set()
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
        if self.discovery_events_path is not None:
            # --observe-via-file mode owns no observe child: the shared discovery
            # log's writer (another `mngr observe`) re-emits a fresh snapshot that
            # the tailer picks up on its own, so a bounce here is a no-op.
            logger.debug("bounce_observe: --observe-via-file mode, nothing to bounce")
            return
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
            is_checked_by_group=False,
            # This child streams a discovery snapshot per provider every poll interval
            # for as long as the forward runs, so retaining its output would grow
            # without bound. ``_on_observe_output`` consumes each line as it arrives
            # (logging stderr), and nothing reads the output back.
            is_output_accumulated=False,
        )

    def _start_tail_discovery(self) -> None:
        """Tail the shared discovery events file in-process instead of spawning observe."""
        self._cg.start_new_thread(
            target=self._run_tail_discovery,
            name="mngr-forward-discovery-tail",
            daemon=True,
            is_checked=False,
        )

    def _run_tail_discovery(self) -> None:
        assert self.discovery_events_path is not None
        tail_discovery_events_file(
            events_path=self.discovery_events_path,
            stop_event=self._tail_stop_event,
            on_line=self._process_observe_line,
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
        self._process_observe_line(line)

    def _process_observe_line(self, line: str) -> None:
        """Parse one discovery JSONL line into the envelope + resolver state.

        Shared by the subprocess observe reader (``_on_observe_output``) and the
        ``--observe-via-file`` tailer (``_run_tail_discovery``).
        """
        stripped = line.strip()
        if not stripped:
            return
        # Pass through to the envelope writer regardless of whether we
        # successfully parse the event below — consumers may want to
        # introspect it themselves.
        self.envelope_writer.emit_observe(stripped)
        try:
            event = self._discovery_schema_warner.parse(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse discovery line {!r}: {}", stripped[:200], e)
            return
        if event is None:
            return
        self._handle_discovery_event(event)

    def _handle_discovery_event(self, event: DiscoveryEvent) -> None:
        if isinstance(event, ProviderDiscoverySnapshotEvent):
            self._handle_provider_snapshot(event)
        elif isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, AgentDiscoveryEvent):
            self._handle_agent_discovered(event)
        elif isinstance(event, (HostDiscoveryEvent, AgentDestroyedEvent, HostDestroyedEvent)):
            self._apply_event_and_reconcile(event)
        elif isinstance(event, DiscoveryErrorEvent):
            # Fold into the aggregator so its per-provider error map stays current,
            # then surface the error to the operator (suppressing per-poll repeats
            # of the same provider-level failure).
            self._aggregator.apply_event(event)
            self._error_log_suppressor.log_discovery_error_event(event)
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

    def _handle_provider_snapshot(self, event: ProviderDiscoverySnapshotEvent) -> None:
        # A clean snapshot re-arms the provider's error-log suppression (and logs
        # a recovery line if its error was previously logged).
        self._error_log_suppressor.record_provider_snapshot(event)
        # Drop agents that the plugin's CEL filters exclude before folding the
        # snapshot in, so the aggregator only ever tracks agents we manage.
        filtered_agents = tuple(agent for agent in event.agents if self._agent_passes_filter(agent))
        filtered_event = event.model_copy_update(to_update(event.field_ref().agents, filtered_agents))
        self._apply_event_and_reconcile(filtered_event)
        # A per-agent events stream can die (e.g. its host rebooted and broke the
        # long-lived --follow connection). Nothing respawns it on its own, so the
        # periodic snapshot drives the retry: re-start it for every agent the
        # aggregator still tracks (the call is a no-op for live ones). Restrict this
        # to agents the aggregator kept -- it is span-aware and deliberately does not
        # re-add an agent whose own destroy event landed during this snapshot's span,
        # so restarting a stream from the raw event.agents would resurrect (and never
        # tear down) a stream for an agent already considered gone.
        present_agent_instances = self._aggregator.get_agent_by_instance()
        for agent in filtered_agents:
            if agent.instance_key in present_agent_instances:
                self._start_events_stream(agent.instance_key)

    def _apply_event_and_reconcile(self, event: DiscoveryEvent) -> None:
        """Fold one discovery event into the aggregator and apply the resulting membership delta."""
        delta = self._aggregator.apply_event(event)
        # The aggregator is now the source of truth for the known-agent set; sync
        # the resolver to its full (all-provider) view so a per-provider snapshot
        # never clobbers agents owned by other providers.
        self.resolver.update_known_agents(tuple(self._aggregator.get_agent_by_instance()))
        self._apply_membership_delta(delta)

    def _apply_membership_delta(self, delta: AggregatorDelta) -> None:
        for host_id_str in delta.removed_host_ids:
            with self._lock:
                self._ssh_by_host_id.pop(host_id_str, None)
        for instance_key in delta.removed_agent_instances:
            self._teardown_agent(instance_key)
        for instance_key in delta.added_agent_instances:
            self._setup_agent(instance_key)

    def _setup_agent(self, instance_key: AgentInstanceKey) -> None:
        ssh_info = self._ssh_for_agent(instance_key)
        if ssh_info is not None:
            self.resolver.update_ssh_info(instance_key, ssh_info)
        self._start_events_stream(instance_key)
        provider_name = self._provider_name_for_agent(instance_key)
        for callback in self._on_agent_discovered_callbacks:
            self._safely_call(callback, instance_key.agent_id, ssh_info, provider_name, name="on_agent_discovered")

    def _teardown_agent(self, instance_key: AgentInstanceKey) -> None:
        with self._lock:
            self._events_services.pop(str(instance_key), None)
            self._events_labels.pop(str(instance_key), None)
        self._stop_events_stream(instance_key)
        for callback in self._on_agent_destroyed_callbacks:
            self._safely_call(callback, instance_key.agent_id, name="on_agent_destroyed")

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = RemoteSSHInfo(
            user=event.ssh.user,
            host=event.ssh.host,
            port=event.ssh.port,
            key_path=event.ssh.key_path,
            known_hosts_path=event.ssh.known_hosts_path,
        )
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
        self._aggregator.apply_event(event)
        agents_on_host = [agent for agent in self._aggregator.get_agents() if str(agent.host_id) == host_id_str]

        for agent in agents_on_host:
            self.resolver.update_ssh_info(agent.instance_key, ssh_info)
            for callback in self._on_agent_discovered_callbacks:
                self._safely_call(
                    callback,
                    agent.agent_id,
                    ssh_info,
                    self._provider_name_for_agent(agent.instance_key),
                    name="on_agent_discovered (ssh-info-late)",
                )

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        if not self._agent_passes_filter(event.agent):
            return
        self._apply_event_and_reconcile(event)

    def _ssh_for_agent(self, instance_key: AgentInstanceKey) -> RemoteSSHInfo | None:
        with self._lock:
            return self._ssh_by_host_id.get(str(instance_key.host_id))

    def _provider_name_for_agent(self, instance_key: AgentInstanceKey) -> str:
        agent = self._aggregator.get_agent_by_instance().get(instance_key)
        if agent is None:
            return "unknown"
        return str(agent.provider_name)

    # -- per-agent events streams -----------------------------------------

    def _start_events_stream(self, instance_key: AgentInstanceKey) -> None:
        if self._cg.is_shutting_down():
            return
        if not self._filtered_event_sources:
            # Either ``event_sources`` was empty to begin with, or every
            # source was filtered out by ``--event-include`` / ``--event-exclude``.
            return
        instance_str = str(instance_key)
        with self._lock:
            existing = self._events_processes.get(instance_str)
            if existing is not None and existing.poll() is None:
                # A live events stream is already running for this agent.
                return
            if existing is not None:
                # The previous stream exited -- most often because the agent's
                # host restarted (e.g. after a reboot) and broke the long-lived
                # ``mngr event ... --follow`` connection, which then exits
                # non-zero. Nothing respawns it on its own, so the resolver's
                # per-agent service map would stay empty forever and
                # ``resolve`` would keep returning None (a permanent 503).
                # Drop the dead entry and respawn below -- but paced by an
                # exponential backoff, not the raw discovery snapshot cadence:
                # a stream that dies instantly (unreachable host) respawned
                # every snapshot is a reconnect storm, and against an sshd
                # with per-source penalties that storm sustains the penalty
                # window forever, locking this machine out of a healthy VM.
                now = time.monotonic()
                # Judge healthiness once, at the first snapshot that notices
                # the death (popping spawned_at makes later snapshots skip
                # this check). Re-evaluating on every snapshot would let a
                # corpse waiting out a 60s window "age into" healthiness and
                # reset the ladder even though the stream died instantly.
                spawned_at = self._events_spawned_at_by_instance.pop(instance_str, None)
                if spawned_at is not None and now - spawned_at >= _EVENTS_STREAM_HEALTHY_AGE_SECONDS:
                    # The dead stream had lived long enough to count as
                    # healthy; treat this exit as a fresh incident. That clears
                    # the gone-target strikes too: a stream that ran healthily
                    # for a minute proves its target existed, so an earlier
                    # strike must not combine with a later one hours apart to
                    # sideline a live agent for the long backoff.
                    self._events_respawn_backoff_by_instance.pop(instance_str, None)
                    self._events_gone_target_strikes_by_instance.pop(instance_str, None)
                if now < self._events_next_respawn_at_by_instance.get(instance_str, 0.0):
                    # Still inside the backoff window: keep the dead entry so a
                    # later snapshot retries once the window has passed.
                    return
                stderr_tail = tuple(self._events_stderr_tail_by_instance.pop(instance_str, ()))
                stderr_summary = " | ".join(stderr_tail) if stderr_tail else "(no stderr captured)"
                # The child exits with a distinct code when its target does not
                # exist, so this needs no message matching. See
                # EXIT_CODE_TARGET_NOT_FOUND in mngr's errors module.
                if existing.returncode == EXIT_CODE_TARGET_NOT_FOUND:
                    gone_strikes = self._events_gone_target_strikes_by_instance.get(instance_str, 0) + 1
                    self._events_gone_target_strikes_by_instance[instance_str] = gone_strikes
                else:
                    gone_strikes = 0
                    self._events_gone_target_strikes_by_instance.pop(instance_str, None)
                if gone_strikes >= _EVENTS_GONE_TARGET_STRIKES_REQUIRED:
                    # A flat, much longer interval that neither consumes nor
                    # escalates the crash-loop ladder, so a later ordinary crash
                    # resumes the ladder where this interlude left it.
                    backoff = _EVENTS_GONE_TARGET_RESPAWN_BACKOFF_SECONDS
                    logger.info(
                        "Per-agent events stream for {} exited (returncode={}) reporting its target gone "
                        "{} times running; retrying no sooner than {:.0f}s in case discovery is stale; "
                        "last stderr: {}",
                        instance_key,
                        existing.returncode,
                        gone_strikes,
                        backoff,
                        stderr_summary,
                    )
                else:
                    backoff = self._events_respawn_backoff_by_instance.get(
                        instance_str, _EVENTS_RESPAWN_INITIAL_BACKOFF_SECONDS
                    )
                    self._events_respawn_backoff_by_instance[instance_str] = min(
                        backoff * 2, _EVENTS_RESPAWN_MAX_BACKOFF_SECONDS
                    )
                    logger.info(
                        "Per-agent events stream for {} exited (returncode={}); respawning "
                        "(next retry no sooner than {:.0f}s); last stderr: {}",
                        instance_key,
                        existing.returncode,
                        backoff,
                        stderr_summary,
                    )
                self._events_next_respawn_at_by_instance[instance_str] = now + backoff
                self._events_processes.pop(instance_str, None)
            # Preserve any already-known services across a respawn (the new
            # stream re-emits current registrations on connect); only seed an
            # empty map on the first spawn.
            self._events_services.setdefault(instance_str, {})
        sources: Sequence[str] = self._filtered_event_sources
        try:
            # The instance key doubles as a host-scoped CLI address
            # (``<agent_id>@<host_id>``), so this stream stays unambiguous even
            # when the same agent id exists on another host mid-migration.
            process = self._cg.run_process_in_background(
                command=[
                    self.mngr_binary,
                    "event",
                    instance_str,
                    *sources,
                    "--follow",
                    "--quiet",
                ],
                on_output=lambda line, is_stdout, _key=instance_key: self._on_event_output(line, is_stdout, _key),
                cwd=Path.home(),
                is_checked_by_group=False,
                # A ``--follow`` stream lives as long as its agent does, so retaining
                # every event it ever emits would grow without bound. Lines are consumed
                # on arrival by ``_on_event_output``, which also logs stderr.
                is_output_accumulated=False,
            )
            with self._lock:
                self._events_processes[instance_str] = process
                self._events_spawned_at_by_instance[instance_str] = time.monotonic()
        except InvalidConcurrencyGroupStateError:
            logger.debug("Skipping events stream for {} -- concurrency group inactive", instance_key)

    def _stop_events_stream(self, instance_key: AgentInstanceKey) -> None:
        instance_str = str(instance_key)
        with self._lock:
            process = self._events_processes.pop(instance_str, None)
            self._events_respawn_backoff_by_instance.pop(instance_str, None)
            self._events_next_respawn_at_by_instance.pop(instance_str, None)
            self._events_spawned_at_by_instance.pop(instance_str, None)
            self._events_stderr_tail_by_instance.pop(instance_str, None)
            self._events_gone_target_strikes_by_instance.pop(instance_str, None)
        if process is None:
            return
        try:
            process.terminate()
        except (OSError, RuntimeError) as e:
            logger.trace("Error terminating events stream for {}: {}", instance_key, e)

    def _on_event_output(self, line: str, is_stdout: bool, instance_key: AgentInstanceKey) -> None:
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr event stderr for {}: {}", instance_key, stripped)
                with self._lock:
                    tail = self._events_stderr_tail_by_instance.setdefault(
                        str(instance_key), deque(maxlen=_EVENTS_STDERR_TAIL_MAX_LINES)
                    )
                    tail.append(stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        self.envelope_writer.emit_event(instance_key.agent_id, stripped)

        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Could not parse event line for {}: {}", instance_key, e)
            return
        if not isinstance(raw, dict):
            return
        source = raw.get("source")
        if source != _SERVICES_SOURCE:
            # Request events are passed through to consumers via the
            # envelope; the plugin doesn't consume them itself.
            return

        event_type = raw.get("type", "service_registered")
        service = raw.get("service")
        if not isinstance(service, str) or not service:
            return

        instance_str = str(instance_key)
        with self._lock:
            services = self._events_services.setdefault(instance_str, {})
            labels = self._events_labels.setdefault(instance_str, {})
            if event_type == "service_deregistered":
                services.pop(service, None)
                labels.pop(service, None)
            else:
                url = raw.get("url")
                if isinstance(url, str) and url:
                    services[service] = url
                # The origin label routes ``<label>.host-<hex>`` to this
                # service; fall back to the name when a (legacy) event omits it.
                # CLEANUP: drop the bare-name fallback (require the label) once
                # no supported host's services event log predates the first
                # release whose registration script (forward_port.py) mints
                # ``<name>-<rand>`` origin labels -- services re-register (and
                # mint) on boot, so any host booted on a labeling release is
                # labeled.
                label = raw.get("label")
                labels[service] = label if isinstance(label, str) and label else service
            services_snapshot = dict(services)
            # Invert to origin-label -> service-name for the resolver's routing.
            label_to_name_snapshot = {label: name for name, label in labels.items()}
        self.resolver.update_services(instance_key, services_snapshot, label_to_name_snapshot)

    @staticmethod
    def _safely_call(callback: Callable[..., None], *args: Any, name: str) -> None:
        try:
            callback(*args)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("{} callback failed: {}", name, e)
