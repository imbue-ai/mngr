"""Minimal ``mngr observe``-driven discovery dispatcher for ``mngr latchkey forward``.

Spawns a single ``mngr observe --discovery-only --quiet`` subprocess,
parses each JSONL line into a discovery event, and fans the relevant
events out to the two latchkey lifecycle handlers
(:class:`LatchkeyDiscoveryHandler` /
:class:`LatchkeyDestructionHandler`).

This is a stripped-down sibling of ``imbue.mngr_forward.stream_manager.ForwardStreamManager``:
it deliberately does *not* spawn per-agent ``mngr event`` subprocesses,
does *not* track service URLs, does *not* feed a resolver, and does
*not* write JSONL envelopes to stdout. The plugin only needs to know
when agents come and go and what their SSH info is, which is exactly
what the ``observe`` discovery stream already carries.

SSH info arrival order is handled the same way ``ForwardStreamManager``
handles it: an agent can be discovered before its host's
``HOST_SSH_INFO`` event arrives, so we re-fire the discovery callback
for every agent on a host whenever that host's SSH info first becomes
available. The downstream :class:`LatchkeyDiscoveryHandler` is already
designed to coalesce duplicate fires for the same agent and to be a
no-op when ``ssh_info is None``.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import DiscoveryEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import SSHInfo
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_latchkey.core import LatchkeyError


class DiscoveryStreamError(LatchkeyError, RuntimeError):
    """Raised when the discovery stream consumer is used incorrectly."""


# Bare-name default for the ``mngr`` CLI; callers can pass an absolute
# path via the ``mngr_binary`` field for environments where ``mngr`` is
# not on ``PATH``.
MNGR_BINARY: Final[str] = "mngr"

# Failure modes that bouncing the observe subprocess can surface from the
# ``ConcurrencyGroup`` -- both tearing the old child down (``terminate``)
# and spawning the replacement (``run_process_in_background``). Beyond a
# plain ``OSError`` / ``RuntimeError``, a force-kill that overruns its
# grace period raises ``TimeoutExpired``, and the group's strand/shutdown
# checks raise ``ConcurrencyGroupError`` or wrap failures in a
# ``ConcurrencyExceptionGroup``. A bounce runs on the long-lived SIGHUP
# watcher thread, so it must treat any of these as "the bounce did not
# complete" (log and carry on) rather than let an unexpected type escape
# and kill the watcher, which would silently disable every later refresh.
_OBSERVE_BOUNCE_ERRORS: Final = (
    OSError,
    RuntimeError,
    TimeoutExpired,
    ConcurrencyGroupError,
    ConcurrencyExceptionGroup,
)

# Callback signatures fired by the consumer for every observe-stream
# transition that matters to the plugin. Tuples instead of bespoke
# pydantic types so test doubles can pass arbitrary callables without
# having to subclass the production discovery handler (which carries
# its own required fields).
OnAgentDiscoveredCallback = Callable[[AgentId, HostId, RemoteSSHInfo | None, str], None]
OnAgentDestroyedCallback = Callable[[AgentId], None]


def _convert_ssh_info(ssh: SSHInfo) -> RemoteSSHInfo:
    """Project the discovery-stream :class:`SSHInfo` onto the plugin's :class:`RemoteSSHInfo`.

    The two types carry the same fields except for ``command`` (which the
    plugin's reverse-tunnel manager doesn't need); the conversion is
    therefore purely a field-by-field copy.
    """
    return RemoteSSHInfo(
        user=ssh.user,
        host=ssh.host,
        port=ssh.port,
        key_path=ssh.key_path,
    )


class DiscoveryStreamConsumer(MutableModel):
    """Consume the ``mngr observe`` discovery stream and dispatch agent lifecycle events."""

    concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description="ConcurrencyGroup that owns the observe subprocess and the dispatch thread.",
    )
    mngr_binary: str = Field(
        default=MNGR_BINARY,
        frozen=True,
        description="Path to the mngr binary used to spawn the observe subprocess.",
    )
    loadable_provider_names: frozenset[str] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Names of the providers this forward's config would actually load (from "
            "``list_provider_names_to_load``). A provider disabled in config (e.g. via the "
            "minds providers panel setting ``is_enabled = false``) is absent from this set. "
            "Discovery errors attributable to such a provider are expected noise -- other mngr "
            "processes (e.g. ``mngr list``) still write them to the shared discovery log, which "
            "this forward's ``mngr observe`` tail echoes -- so they are logged at trace rather "
            "than warning. ``None`` disables the filter and logs every discovery error at warning "
            "(the pre-filter behaviour, used by tests that do not exercise the disabled-provider path)."
        ),
    )

    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # Single source of truth for which agents/hosts are present. Folds every
    # parsed discovery event (per-provider snapshots plus incrementals) into one
    # span-aware, per-provider-scoped view, and reports the membership delta we
    # turn into discovered/destroyed callbacks. The agent's host_id and
    # provider_name are read back from its DiscoveredAgent rather than tracked
    # separately here.
    _aggregator: DiscoveryStateAggregator = PrivateAttr(default_factory=DiscoveryStateAggregator)
    # host_id_str -> SSH info. The aggregator does not retain SSH connection
    # info, so we keep it here to re-fire the discovery callback (with the SSH
    # info now available) for every agent on a host when its HostSSHInfoEvent
    # arrives after the agents were discovered.
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _process: RunningProcess | None = PrivateAttr(default=None)

    def add_on_agent_discovered_callback(self, callback: OnAgentDiscoveredCallback) -> None:
        """Register a callback fired for every agent discovered (or re-fired on late SSH info)."""
        self._on_agent_discovered_callbacks.append(callback)

    def add_on_agent_destroyed_callback(self, callback: OnAgentDestroyedCallback) -> None:
        """Register a callback fired for every agent destruction observed in the stream."""
        self._on_agent_destroyed_callbacks.append(callback)

    def _observe_command(self) -> list[str]:
        """Build the ``mngr observe`` argv."""
        return [self.mngr_binary, "observe", "--discovery-only", "--quiet"]

    def start(self) -> None:
        """Spawn the ``mngr observe`` subprocess and begin dispatching events."""
        if self._process is not None:
            raise DiscoveryStreamError("DiscoveryStreamConsumer.start already called")
        # ``is_checked_by_group=False``: this is a long-running stream that we
        # stop explicitly via ``.terminate()`` (on bounce and on shutdown),
        # which delivers SIGTERM and yields a non-zero exit code. Left checked,
        # that deliberate teardown would surface as a ``ProcessError`` ->
        # ``ConcurrencyExceptionGroup`` the next time the group inspects its
        # strands (e.g. the respawn below, or supervisor exit), turning every
        # intentional bounce into a spurious failure.
        self._process = self.concurrency_group.run_process_in_background(
            command=self._observe_command(),
            on_output=self._on_observe_output,
            cwd=Path.home(),
            is_checked_by_group=False,
        )

    def bounce_observe(self) -> None:
        """Terminate and respawn the ``mngr observe`` subprocess only.

        Cached discovery state (known agents, host SSH info, provider names)
        and registered callbacks all survive, so the shared gateway and any
        existing reverse tunnels are untouched; the restarted observe re-emits
        a fresh snapshot that reflects the current provider set. Used by the
        ``mngr latchkey forward`` SIGHUP handler so mid-session provider
        changes take effect without restarting the whole supervisor. No-op if
        the observe subprocess was never started.
        """
        if self._process is None:
            logger.debug("bounce_observe: no observe process running; skipping")
            return
        logger.info("Bouncing mngr observe subprocess")
        try:
            self._process.terminate()
        except _OBSERVE_BOUNCE_ERRORS as e:
            logger.warning("Failed to terminate observe process during bounce: {}", e)
        # ``is_checked_by_group=False`` for the same reason as ``start``: the
        # replacement stream is also stopped explicitly, so its eventual
        # SIGTERM exit must not be checked.
        try:
            self._process = self.concurrency_group.run_process_in_background(
                command=self._observe_command(),
                on_output=self._on_observe_output,
                cwd=Path.home(),
                is_checked_by_group=False,
            )
        except _OBSERVE_BOUNCE_ERRORS as e:
            logger.warning("Failed to respawn observe process during bounce: {}", e)
            self._process = None

    def stop(self) -> None:
        """Terminate the ``mngr observe`` subprocess."""
        if self._process is None:
            return
        try:
            self._process.terminate()
        except _OBSERVE_BOUNCE_ERRORS as e:
            # Same terminate() failure modes as a bounce (notably TimeoutExpired
            # on a force-kill overrun): swallow them so the rest of the forward
            # shutdown sequence (tunnel cleanup, gateway stop, forward-info
            # deletion) still runs instead of being aborted mid-teardown.
            logger.trace("Error terminating observe subprocess: {}", e)
        self._process = None

    # -- output handling -------------------------------------------------

    def _on_observe_output(self, line: str, is_stdout: bool) -> None:
        if not is_stdout:
            stripped = line.strip()
            if stripped:
                logger.debug("mngr observe stderr: {}", stripped)
            return
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = parse_discovery_event_line(stripped)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("Failed to parse discovery line {!r}: {}", stripped[:200], e)
            return
        if event is None:
            return
        self._handle_discovery_event(event)

    def _handle_discovery_event(self, event: DiscoveryEvent) -> None:
        # Fold every event into the shared aggregator first; it is the single
        # source of truth for membership and reports exactly which agents
        # appeared or disappeared (per-provider scoped, span-aware, and with
        # provider-error retention all handled internally). We then turn that
        # delta into destruction callbacks and fire discovery callbacks for the
        # agents this event carries.
        delta = self._aggregator.apply_event(event)
        for aid_str in delta.removed_agent_ids:
            self._safely_call_destroyed(AgentId(aid_str))

        if isinstance(event, ProviderDiscoverySnapshotEvent):
            # Fire discovered only for snapshot agents the aggregator actually kept.
            # It is span-aware: an agent whose own destroy/state-change event landed
            # during this snapshot's discovery span is deliberately not re-added, so
            # firing discovered from the raw event.agents would re-establish a reverse
            # tunnel for an agent the aggregator already considers gone.
            present_agent_ids = self._aggregator.get_agent_by_id()
            for agent in event.agents:
                if str(agent.agent_id) in present_agent_ids:
                    self._fire_discovered(agent)
        elif isinstance(event, AgentDiscoveryEvent):
            self._fire_discovered(event.agent)
        elif isinstance(event, HostSSHInfoEvent):
            self._handle_host_ssh_info(event)
        elif isinstance(event, HostDestroyedEvent):
            with self._lock:
                self._ssh_by_host_id.pop(str(event.host_id), None)
        elif isinstance(event, DiscoveryErrorEvent):
            if self._is_error_from_disabled_provider(event):
                # A provider the user disabled in this forward's config: its discovery errors
                # (written to the shared discovery log by other mngr processes such as
                # ``mngr list``, whose tail this forward echoes) are expected noise about a
                # provider we intentionally do not manage, so drop them to trace instead of
                # spamming the forward log at warning.
                logger.trace(
                    "Ignoring discovery error from disabled provider {}: {} ({})",
                    event.provider_name,
                    event.error_message,
                    event.error_type,
                )
            else:
                logger.warning(
                    "Discovery error from {}: {} ({})",
                    event.source_name,
                    event.error_message,
                    event.error_type,
                )
        else:
            # Remaining event types (AgentDestroyedEvent, HostDiscoveryEvent, and
            # the ignored legacy FullDiscoverySnapshotEvent) need no extra work
            # here: destruction callbacks were already fired from the delta above,
            # and these events carry nothing requiring a discovery callback.
            logger.trace("No discovery callback to fire for event of type {}", type(event).__name__)

    def _is_error_from_disabled_provider(self, event: DiscoveryErrorEvent) -> bool:
        """Whether this discovery error is attributable to a provider disabled in our config.

        Only provider-attributable errors (``provider_name`` set) can be suppressed; a
        non-provider error (host/agent, ``provider_name is None``) is always surfaced.
        With no loadable-provider set configured (``None``) the filter is inert.
        """
        if self.loadable_provider_names is None:
            return False
        if event.provider_name is None:
            return False
        return event.provider_name not in self.loadable_provider_names

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = _convert_ssh_info(event.ssh)
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
        # Re-fire the discovery callback for every agent on this host so
        # ``LatchkeyDiscoveryHandler`` can set up the reverse tunnel now that
        # SSH info is finally available. The aggregator is the authoritative
        # record of which agents are currently on this host.
        agents_on_host = [agent for agent in self._aggregator.get_agents() if str(agent.host_id) == host_id_str]
        for agent in agents_on_host:
            self._safely_call_discovered(agent.agent_id, agent.host_id, ssh_info, str(agent.provider_name))

    def _fire_discovered(self, agent: DiscoveredAgent) -> None:
        ssh_info = self._ssh_for_host(agent.host_id)
        self._safely_call_discovered(agent.agent_id, agent.host_id, ssh_info, str(agent.provider_name))

    def _ssh_for_host(self, host_id: HostId) -> RemoteSSHInfo | None:
        with self._lock:
            return self._ssh_by_host_id.get(str(host_id))

    def _safely_call_discovered(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        for callback in self._on_agent_discovered_callbacks:
            try:
                callback(agent_id, host_id, ssh_info, provider_name)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_discovered callback failed for {}: {}", agent_id, e)

    def _safely_call_destroyed(self, agent_id: AgentId) -> None:
        for callback in self._on_agent_destroyed_callbacks:
            try:
                callback(agent_id)
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("on_agent_destroyed callback failed for {}: {}", agent_id, e)
