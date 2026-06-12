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
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.discovery_events import partition_removed_agents_by_provider_error
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

    _on_agent_discovered_callbacks: list[OnAgentDiscoveredCallback] = PrivateAttr(default_factory=list)
    _on_agent_destroyed_callbacks: list[OnAgentDestroyedCallback] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # agent_id_str -> host_id_str. Tracked so we can look up the SSH
    # info of an agent's host as soon as a HostSSHInfoEvent arrives.
    _host_id_by_agent_id: dict[str, str] = PrivateAttr(default_factory=dict)
    _ssh_by_host_id: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    # agent_id_str -> provider_name. Cached so we can re-fire the
    # discovery callback (after a late HostSSHInfoEvent) without losing
    # the provider name that came with the original AgentDiscoveryEvent.
    _provider_by_agent_id: dict[str, str] = PrivateAttr(default_factory=dict)
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
        self._process = self.concurrency_group.run_process_in_background(
            command=self._observe_command(),
            on_output=self._on_observe_output,
            cwd=Path.home(),
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
        except (OSError, RuntimeError) as e:
            logger.warning("Failed to terminate observe process during bounce: {}", e)
        try:
            self._process = self.concurrency_group.run_process_in_background(
                command=self._observe_command(),
                on_output=self._on_observe_output,
                cwd=Path.home(),
            )
        except (OSError, RuntimeError) as e:
            logger.warning("Failed to respawn observe process during bounce: {}", e)
            self._process = None

    def stop(self) -> None:
        """Terminate the ``mngr observe`` subprocess."""
        if self._process is None:
            return
        try:
            self._process.terminate()
        except (OSError, RuntimeError) as e:
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

    def _handle_full_snapshot(self, event: FullDiscoverySnapshotEvent) -> None:
        # Snapshots replace the entire known agent set, but only for providers
        # that succeeded this poll: an agent absent because its provider errored
        # is retained (its reverse tunnel stays up) rather than torn down. We
        # fire the destruction callback only for genuinely-dropped agents, then
        # fire the discovery callback for every agent in the new snapshot.
        new_agents: dict[str, DiscoveredAgent] = {str(agent.agent_id): agent for agent in event.agents}
        with self._lock:
            prior_host_id_by_agent_id = dict(self._host_id_by_agent_id)
            prior_provider_by_agent_id = dict(self._provider_by_agent_id)
            removed = prior_host_id_by_agent_id.keys() - new_agents.keys()
            partition = partition_removed_agents_by_provider_error(
                removed_agent_ids=removed,
                provider_name_by_prior_agent_id=prior_provider_by_agent_id,
                error_by_provider_name=event.error_by_provider_name,
            )
            new_host_id_by_agent_id = {aid_str: str(agent.host_id) for aid_str, agent in new_agents.items()}
            new_provider_by_agent_id = {aid_str: str(agent.provider_name) for aid_str, agent in new_agents.items()}
            # Carry retained agents forward from prior state so they keep their
            # host/provider mapping and survive the next snapshot's diff too.
            for aid_str in partition.retained:
                new_host_id_by_agent_id[aid_str] = prior_host_id_by_agent_id[aid_str]
                new_provider_by_agent_id[aid_str] = prior_provider_by_agent_id[aid_str]
            self._host_id_by_agent_id = new_host_id_by_agent_id
            self._provider_by_agent_id = new_provider_by_agent_id

        if partition.retained:
            logger.debug(
                "Retained {} agent(s) through a provider discovery error; keeping their reverse tunnels: {}",
                len(partition.retained),
                sorted(partition.retained),
            )
        for aid_str in partition.dropped:
            self._safely_call_destroyed(AgentId(aid_str))

        for agent in new_agents.values():
            ssh_info = self._ssh_for_agent(agent.agent_id)
            self._safely_call_discovered(agent.agent_id, agent.host_id, ssh_info, str(agent.provider_name))

    def _handle_host_ssh_info(self, event: HostSSHInfoEvent) -> None:
        ssh_info = _convert_ssh_info(event.ssh)
        host_id_str = str(event.host_id)
        with self._lock:
            self._ssh_by_host_id[host_id_str] = ssh_info
            agents_on_host = [
                (AgentId(aid_str), self._provider_by_agent_id.get(aid_str, "unknown"))
                for aid_str, hid_str in self._host_id_by_agent_id.items()
                if hid_str == host_id_str
            ]
        # Re-fire the discovery callback for every agent on this host so
        # ``LatchkeyDiscoveryHandler`` can set up the reverse tunnel
        # now that SSH info is finally available.
        for agent_id, provider_name in agents_on_host:
            self._safely_call_discovered(agent_id, HostId(host_id_str), ssh_info, provider_name)

    def _handle_agent_discovered(self, event: AgentDiscoveryEvent) -> None:
        agent = event.agent
        aid_str = str(agent.agent_id)
        with self._lock:
            self._host_id_by_agent_id[aid_str] = str(agent.host_id)
            self._provider_by_agent_id[aid_str] = str(agent.provider_name)
        ssh_info = self._ssh_for_agent(agent.agent_id)
        self._safely_call_discovered(agent.agent_id, agent.host_id, ssh_info, str(agent.provider_name))

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
            self._host_id_by_agent_id.pop(aid_str, None)
            self._provider_by_agent_id.pop(aid_str, None)
        self._safely_call_destroyed(agent_id)

    def _ssh_for_agent(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        with self._lock:
            host_id = self._host_id_by_agent_id.get(str(agent_id))
            if host_id is None:
                return None
            return self._ssh_by_host_id.get(host_id)

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
