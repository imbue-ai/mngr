"""Resolves ``[<service>.]host-<hex>.localhost`` requests to a backend ``ProxyTarget``.

Holds four pieces of state, all updated externally:

- The configured forwarding strategy: either ``ForwardServiceStrategy`` (look
  up a named service URL per agent) or ``ForwardPortStrategy`` (forward to a
  fixed remote port on the agent's host).
- ``services_by_agent``: per-agent service-name → URL, populated from the
  ``mngr event`` stream's ``services`` source.
- ``ssh_by_agent``: per-agent SSH info, populated from the ``mngr observe``
  stream's ``HOST_SSH_INFO`` events; absent for local agents.
- ``host_by_agent``: which host each known agent runs on, populated from
  discovery (or the ``--no-observe`` snapshot). Hostnames name *hosts*, so
  ``resolve_agent_for_host`` maps the Host-header coordinate back to the
  agent whose registered services should serve it.

``resolve(agent_id, service_name)`` returns ``None`` when the agent is
unknown, the requested service URL is not yet discovered, or the agent has
no SSH info but the strategy requires one.
"""

import threading
from typing import assert_never

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.data_types import BackendUrl
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.data_types import ForwardStrategy
from imbue.mngr_forward.data_types import ProxyTarget
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.service_map_cache import ServiceMapCache
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


class ForwardResolver(MutableModel):
    """Maps an agent ID to its current backend ``ProxyTarget``."""

    strategy: ForwardStrategy = Field(
        frozen=True,
        description="Either ForwardServiceStrategy or ForwardPortStrategy; chosen at CLI parse time",
    )
    envelope_writer: EnvelopeWriter | None = Field(
        default=None,
        description=(
            "Optional writer used to emit a ``resolver_snapshot`` envelope on every "
            "mutation of the per-agent services map -- ``update_services`` "
            "(set/replace) plus the destruction paths (``remove_known_agent`` and "
            "``update_known_agents`` when they drop an agent that had services). "
            "The plugin wires this so a downstream consumer can mirror the per-agent "
            "service map. None in tests / code paths that don't care about emission."
        ),
    )
    service_map_cache: ServiceMapCache | None = Field(
        default=None,
        description=(
            "Optional last-known service-map cache. When set, every mutation of "
            "the per-agent services map (the same points that emit "
            "``resolver_snapshot``) is persisted through it, and ``seed_services`` "
            "loads from it at startup so a fresh run resolves without waiting on "
            "the slow per-agent event stream. None in tests / paths that don't persist."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Per-agent origin label -> service name. Origins are ``<label>.host-<hex>``
    # where the label is unguessable (``<name>-<rand>``); grants and the backend
    # service map stay keyed by name, so an incoming label is mapped back here.
    # A service with no distinct label routes under its own name (label == name).
    _label_to_name_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _ssh_by_agent: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _host_by_agent: dict[str, str] = PrivateAttr(default_factory=dict)
    _known_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _initial_discovery_done: bool = PrivateAttr(default=False)

    def _snapshot_services_locked(self) -> dict[str, dict[str, str]]:
        """Return a deep copy of ``_services_by_agent`` for emission.

        Caller MUST hold ``self._lock``. The copy is taken under the lock
        so the resulting payload is a consistent point-in-time view; the
        actual ``emit_resolver_snapshot`` call must happen outside the
        lock to avoid holding it across a write.
        """
        return {aid: dict(svc) for aid, svc in self._services_by_agent.items()}

    def update_known_agents(self, agent_ids: tuple[AgentId, ...]) -> None:
        """Replace the set of known agents. Drops services / SSH info for removed agents.

        Emits a ``resolver_snapshot`` envelope when any agent's services
        entry was dropped, so consumers stay in sync with the resolver
        after bulk destruction.
        """
        snapshot: dict[str, dict[str, str]] | None = None
        with self._lock:
            new_set = {str(aid) for aid in agent_ids}
            removed = self._known_agent_ids - new_set
            services_changed = False
            for aid_str in removed:
                if self._services_by_agent.pop(aid_str, None) is not None:
                    services_changed = True
                self._ssh_by_agent.pop(aid_str, None)
                self._host_by_agent.pop(aid_str, None)
                self._label_to_name_by_agent.pop(aid_str, None)
            self._known_agent_ids = new_set
            self._initial_discovery_done = True
            if services_changed:
                snapshot = self._snapshot_services_locked()
        if snapshot is not None:
            self._publish_services_snapshot(snapshot)

    def add_known_agent(self, agent_id: AgentId) -> None:
        """Mark a single agent as known (incremental discovery)."""
        with self._lock:
            self._known_agent_ids.add(str(agent_id))
            self._initial_discovery_done = True

    def remove_known_agent(self, agent_id: AgentId) -> None:
        """Mark a single agent as no longer known (incremental destruction).

        Emits a ``resolver_snapshot`` envelope when the agent had a services
        entry (i.e. there was something for the consumer's mirror to drop).
        Mirrors the ``update_services`` emission contract: every mutation
        of ``_services_by_agent`` produces a snapshot envelope so the
        consumer-side mirror does not retain stale entries for destroyed
        agents.
        """
        snapshot: dict[str, dict[str, str]] | None = None
        with self._lock:
            aid_str = str(agent_id)
            self._known_agent_ids.discard(aid_str)
            services_changed = self._services_by_agent.pop(aid_str, None) is not None
            self._ssh_by_agent.pop(aid_str, None)
            self._host_by_agent.pop(aid_str, None)
            self._label_to_name_by_agent.pop(aid_str, None)
            if services_changed:
                snapshot = self._snapshot_services_locked()
        if snapshot is not None:
            self._publish_services_snapshot(snapshot)

    def update_services(self, agent_id: AgentId, services: dict[str, str]) -> None:
        """Replace the known services for a single agent.

        Emits a ``resolver_snapshot`` envelope after the mutation so consumers
        can mirror the per-agent service map. The snapshot carries the full
        per-agent map (not just this agent) so a late-attaching consumer can
        catch up from a single envelope.
        """
        with self._lock:
            self._services_by_agent[str(agent_id)] = dict(services)
            snapshot = self._snapshot_services_locked()
        self._publish_services_snapshot(snapshot)

    def update_service_labels(self, agent_id: AgentId, label_to_name: dict[str, str]) -> None:
        """Replace the known origin-label -> service-name map for a single agent.

        Not emitted or persisted -- labels are re-derived live from the same
        service event stream that feeds ``update_services``.
        """
        with self._lock:
            self._label_to_name_by_agent[str(agent_id)] = dict(label_to_name)

    def resolve_by_origin_label(self, agent_id: AgentId, origin_label: str) -> ProxyTarget | None:
        """Resolve a ``<label>.host-<hex>`` service origin to its backend.

        Maps the (unguessable) origin label back to its service name, then
        resolves by name. A label with no known mapping falls back to being
        treated as the name itself, so a service registered without a distinct
        label (or a plain non-minds agent) still resolves at its own origin.
        """
        with self._lock:
            service_name = self._label_to_name_by_agent.get(str(agent_id), {}).get(origin_label, origin_label)
        return self.resolve(agent_id, service_name)

    def shell_origin_label(self, agent_id: AgentId) -> str | None:
        """The origin label of the configured shell service, for the bare-origin redirect.

        Returns None in port-forward mode (no shell service) or before the
        shell's label has been discovered, in which case the bare origin is
        served directly rather than redirected.
        """
        match self.strategy:
            case ForwardServiceStrategy(service_name=shell_service_name):
                with self._lock:
                    label_to_name = self._label_to_name_by_agent.get(str(agent_id), {})
                    for label, name in label_to_name.items():
                        if name == shell_service_name:
                            return label
                return None
            case ForwardPortStrategy():
                return None
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)
                raise SwitchError(f"Unknown forwarding strategy: {unreachable}")

    def seed_services(self, services_by_agent: dict[str, dict[str, str]]) -> None:
        """Seed the per-agent service map from a last-known cache at startup.

        Fills only the services map; ``resolve()`` still gates on
        discovery-supplied membership, so a seeded entry is served only once
        this run's discovery confirms the agent is known. Does not emit or
        re-persist -- it loads what is already on disk. The resolver is empty at
        startup, so in practice this is a plain fill.
        """
        with self._lock:
            for aid_str, services in services_by_agent.items():
                self._services_by_agent[aid_str] = dict(services)

    def _publish_services_snapshot(self, snapshot: dict[str, dict[str, str]]) -> None:
        """Emit the ``resolver_snapshot`` envelope and persist the service-map cache.

        Called (outside ``self._lock``) at every point that mutates the
        per-agent services map. The envelope keeps a downstream consumer's
        mirror in sync; the cache persists the same full map so a later run can
        seed from it.
        """
        if self.envelope_writer is not None:
            self.envelope_writer.emit_resolver_snapshot(snapshot)
        if self.service_map_cache is not None:
            self.service_map_cache.persist(snapshot)

    def update_ssh_info(self, agent_id: AgentId, ssh_info: RemoteSSHInfo) -> None:
        """Set or replace the SSH info for a single agent."""
        with self._lock:
            self._ssh_by_agent[str(agent_id)] = ssh_info

    def set_agent_host(self, agent_id: AgentId, host_id_str: str) -> None:
        """Record which host an agent runs on (from discovery or a list snapshot)."""
        with self._lock:
            self._host_by_agent[str(agent_id)] = host_id_str

    def resolve_agent_for_host(self, host_id_str: str) -> AgentId | None:
        """Map a Host-header ``host-<hex>`` coordinate to the agent that serves it.

        When several known agents share the host (possible in general, though
        the plugin's CEL filters usually reduce to one per host), the choice
        is deterministic: the lexicographically-smallest agent id wins.
        """
        with self._lock:
            candidates = sorted(
                aid
                for aid, host in self._host_by_agent.items()
                if host == host_id_str and aid in self._known_agent_ids
            )
        if not candidates:
            return None
        return AgentId(candidates[0])

    def get_host_for_agent(self, agent_id: AgentId) -> str | None:
        with self._lock:
            return self._host_by_agent.get(str(agent_id))

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        """All currently-known agent IDs (sorted for stable ordering)."""
        with self._lock:
            return tuple(AgentId(aid) for aid in sorted(self._known_agent_ids))

    def has_completed_initial_discovery(self) -> bool:
        with self._lock:
            return self._initial_discovery_done

    def get_ssh_info(self, agent_id: AgentId) -> RemoteSSHInfo | None:
        with self._lock:
            return self._ssh_by_agent.get(str(agent_id))

    def resolve(self, agent_id: AgentId, service_name: str | None = None) -> ProxyTarget | None:
        """Resolve ``(agent_id, service_name)`` to a backend ``ProxyTarget``, or None if unroutable.

        ``service_name`` is the label parsed from a service origin
        (``<name>.host-<hex>.localhost``); ``None`` means the bare workspace
        origin, which maps to the configured strategy (the shell service in
        service mode, the fixed port in manual mode). A named service is
        looked up directly in the agent's registered service map, so any
        registered service is reachable at its own origin.
        """
        with self._lock:
            aid_str = str(agent_id)
            if aid_str not in self._known_agent_ids:
                return None
            ssh_info = self._ssh_by_agent.get(aid_str)
            services = self._services_by_agent.get(aid_str, {})

        # The bare origin maps to the configured strategy: the shell service in
        # service mode, the fixed port in manual mode.
        if service_name is None:
            match self.strategy:
                case ForwardServiceStrategy(service_name=shell_service_name):
                    service_name = shell_service_name
                case ForwardPortStrategy(remote_port=remote_port):
                    # Manual mode: target ``127.0.0.1:<remote_port>`` on the
                    # agent's host. Local agents reach this directly; remote
                    # agents go via an SSH ``direct-tcpip`` tunnel.
                    url = f"http://127.0.0.1:{remote_port}"
                    return ProxyTarget(url=BackendUrl(url), ssh_info=ssh_info)
                case _ as unreachable:  # pragma: no cover
                    assert_never(unreachable)
                    raise SwitchError(f"Unknown forwarding strategy: {unreachable}")

        url = services.get(service_name)
        if url is None:
            return None
        return ProxyTarget(url=BackendUrl(url), ssh_info=ssh_info)
