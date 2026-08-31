"""Resolves ``[<service>.]agent-<hex>.localhost`` requests to a backend ``ProxyTarget``.

Holds per-instance state, all updated externally:

- The configured forwarding strategy: either ``ForwardServiceStrategy`` (look
  up a named service URL per agent) or ``ForwardPortStrategy`` (forward to a
  fixed remote port on the agent's host).
- ``services_by_instance``: per-agent-instance service-name → URL, populated
  from the ``mngr event`` stream's ``services`` source.
- ``label_to_name_by_instance``: per-agent-instance origin label → service
  name, from that same ``services`` source and set together with
  ``services_by_instance``. An app origin routes by its label, so this is what
  ``resolve_by_origin_label`` reads before resolving by name.
- ``ssh_by_instance``: per-agent-instance SSH info, populated from the
  ``mngr observe`` stream's ``HOST_SSH_INFO`` events; absent for local agents.
- ``known_agent_instances``: the instances discovery has reported. ``resolve``
  gates on this first, so an instance absent from it is unroutable whatever
  its other state says.

Every agent is identified by its :class:`~imbue.mngr.primitives.AgentInstanceKey`
(the ``<agent_id>@<host_id>`` pair): agent ids are unique per host, not
globally, so the same id may exist on multiple hosts (e.g. mid-migration) and
each instance keeps its own services and SSH info. The instance key carries
the host coordinate, which is also how ``resolve_agent_for_host`` maps the
Host-header ``host-<hex>`` coordinate back to the instance whose registered
services should serve it.

``resolve(instance_key, service_name)`` returns ``None`` when the agent is not
yet known to discovery, or when the requested service URL has not been
discovered.
"""

import threading
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.errors import SwitchError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import InvalidAgentInstanceKey
from imbue.mngr_forward.data_types import BackendUrl
from imbue.mngr_forward.data_types import ForwardPortStrategy
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.data_types import ForwardStrategy
from imbue.mngr_forward.data_types import ProxyTarget
from imbue.mngr_forward.service_map_cache import PersistedServiceMap
from imbue.mngr_forward.service_map_cache import ServiceMapCache
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


def _is_valid_instance_key(instance_str: str) -> bool:
    try:
        AgentInstanceKey(instance_str)
    except InvalidAgentInstanceKey:
        logger.debug("Dropping stale service-map cache entry with non-instance key {!r}", instance_str)
        return False
    return True


class ForwardResolver(MutableModel):
    """Maps an agent instance key to its current backend ``ProxyTarget``."""

    strategy: ForwardStrategy = Field(
        frozen=True,
        description="Either ForwardServiceStrategy or ForwardPortStrategy; chosen at CLI parse time",
    )
    service_map_cache: ServiceMapCache | None = Field(
        default=None,
        description=(
            "Optional last-known service-map cache. When set, every mutation of "
            "the per-instance services + label maps -- ``update_services`` (set/replace) plus "
            "the destruction paths (``remove_known_agent`` and ``update_known_agents`` "
            "when they drop an agent that had services or labels) -- is persisted through it, and "
            "``seed_services`` loads from it at startup so a fresh run resolves without "
            "waiting on the slow per-agent event stream. None in tests / paths that don't persist."
        ),
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _services_by_instance: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    # Per-instance origin label -> service name. Origins are ``<label>.host-<hex>``
    # where the label is unguessable (``<name>-<rand>``); grants and the backend
    # service map stay keyed by name, so an incoming label is mapped back here.
    # A service with no distinct label routes under its own name (label == name).
    _label_to_name_by_instance: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _ssh_by_instance: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _known_agent_instances: set[str] = PrivateAttr(default_factory=set)
    _initial_discovery_done: bool = PrivateAttr(default=False)

    def _snapshot_maps_locked(self) -> PersistedServiceMap:
        """Return a deep copy of the per-instance services + label maps for persistence.

        Caller MUST hold ``self._lock``. The copy is taken under the lock so the
        persisted maps are a consistent point-in-time view; the write itself must
        happen outside the lock to avoid holding it across disk I/O.
        """
        return PersistedServiceMap(
            services_by_instance={instance: dict(svc) for instance, svc in self._services_by_instance.items()},
            label_to_name_by_instance={
                instance: dict(labels) for instance, labels in self._label_to_name_by_instance.items()
            },
        )

    def update_known_agents(self, instance_keys: tuple[AgentInstanceKey, ...]) -> None:
        """Replace the set of known agent instances. Drops services / labels / SSH info for removed ones.

        Persists the post-mutation maps when any instance's services or labels
        entry was dropped, so the cache does not seed the next run with agents
        that no longer exist. A bulk destruction persists once for the batch,
        not once per instance.
        """
        snapshot: PersistedServiceMap | None = None
        with self._lock:
            new_set = {str(key) for key in instance_keys}
            removed = self._known_agent_instances - new_set
            maps_changed = False
            for instance_str in removed:
                if self._services_by_instance.pop(instance_str, None) is not None:
                    maps_changed = True
                if self._label_to_name_by_instance.pop(instance_str, None) is not None:
                    maps_changed = True
                self._ssh_by_instance.pop(instance_str, None)
            self._known_agent_instances = new_set
            self._initial_discovery_done = True
            if maps_changed:
                snapshot = self._snapshot_maps_locked()
        if snapshot is not None:
            self._persist_snapshot(snapshot)

    def add_known_agent(self, instance_key: AgentInstanceKey) -> None:
        """Mark a single agent instance as known (incremental discovery)."""
        with self._lock:
            self._known_agent_instances.add(str(instance_key))
            self._initial_discovery_done = True

    def remove_known_agent(self, instance_key: AgentInstanceKey) -> None:
        """Mark a single agent instance as no longer known (incremental destruction).

        Persists the post-mutation maps when the instance had a services or
        labels entry (i.e. there was something to drop). Every mutation of the
        per-instance maps persists, so the cache does not retain stale entries
        for destroyed agents.
        """
        snapshot: PersistedServiceMap | None = None
        with self._lock:
            instance_str = str(instance_key)
            self._known_agent_instances.discard(instance_str)
            maps_changed = False
            if self._services_by_instance.pop(instance_str, None) is not None:
                maps_changed = True
            if self._label_to_name_by_instance.pop(instance_str, None) is not None:
                maps_changed = True
            self._ssh_by_instance.pop(instance_str, None)
            if maps_changed:
                snapshot = self._snapshot_maps_locked()
        if snapshot is not None:
            self._persist_snapshot(snapshot)

    def update_services(
        self, instance_key: AgentInstanceKey, services: dict[str, str], label_to_name: dict[str, str]
    ) -> None:
        """Replace the known services and origin-label map for a single agent instance.

        The two maps update together under one lock acquisition because they
        come from the same service event stream and routing needs them to
        agree: an app origin resolves by looking its label up in one and the
        resulting name up in the other. Persists the post-mutation maps, which
        carry every instance rather than just this one, so the persisted cache
        is a complete point-in-time view a later run can seed from in a single
        read.
        """
        with self._lock:
            instance_str = str(instance_key)
            self._services_by_instance[instance_str] = dict(services)
            self._label_to_name_by_instance[instance_str] = dict(label_to_name)
            snapshot = self._snapshot_maps_locked()
        self._persist_snapshot(snapshot)

    def resolve_by_origin_label(self, instance_key: AgentInstanceKey, origin_label: str) -> ProxyTarget | None:
        """Resolve a ``<label>.host-<hex>`` service origin to its backend.

        Maps the (unguessable) origin label back to its service name, then
        resolves by name. A label with no known mapping falls back to being
        treated as the name itself, so a service registered without a distinct
        label (or a plain non-minds agent) still resolves at its own origin.
        """
        with self._lock:
            service_name = self._label_to_name_by_instance.get(str(instance_key), {}).get(origin_label, origin_label)
        return self.resolve(instance_key, service_name)

    def is_shell_target(self, instance_key: AgentInstanceKey, origin_label: str | None) -> bool:
        """Whether a request with this origin label (None = bare origin) routes to the shell service.

        Used by the legacy ``/service/<name>/`` redirect to fire only on requests
        the shell itself would serve: the bare workspace origin, or a label that
        maps (directly or via the identity fallback) to the shell service name.
        Port-forward mode has no shell, so nothing is a shell target there.
        """
        match self.strategy:
            case ForwardServiceStrategy(service_name=shell_service_name):
                if origin_label is None:
                    return True
                with self._lock:
                    mapped_name = self._label_to_name_by_instance.get(str(instance_key), {}).get(
                        origin_label, origin_label
                    )
                return mapped_name == shell_service_name
            case ForwardPortStrategy():
                return False
            case _ as unreachable:
                assert_never(unreachable)
                raise SwitchError(f"Unknown forwarding strategy: {unreachable}")

    def shell_origin_label(self, instance_key: AgentInstanceKey) -> str | None:
        """The origin label of the configured shell service, for the bare-origin redirect.

        Returns None in port-forward mode (no shell service) or before the
        shell's label has been discovered, in which case the bare origin is
        served directly rather than redirected.
        """
        match self.strategy:
            case ForwardServiceStrategy(service_name=shell_service_name):
                with self._lock:
                    label_to_name = self._label_to_name_by_instance.get(str(instance_key), {})
                    for label, name in label_to_name.items():
                        if name == shell_service_name:
                            return label
                return None
            case ForwardPortStrategy():
                return None
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)
                raise SwitchError(f"Unknown forwarding strategy: {unreachable}")

    def seed_services(self, persisted: PersistedServiceMap) -> None:
        """Seed the per-instance service + label maps from a last-known cache at startup.

        Fills only these two maps; ``resolve()`` still gates on
        discovery-supplied membership, so a seeded entry is served only once
        this run's discovery confirms the agent is known. Labels are seeded so
        app origins (which route by their ``<name>-<rand>`` label) resolve from
        the seed too, not just the shell. Does not emit or re-persist -- it
        loads what is already on disk. The resolver is empty at startup, so in
        practice this is a plain fill.

        Keys that do not parse as instance keys (e.g. bare agent ids persisted
        by an older cache format) are dropped: they could never match a
        discovery-supplied instance, so seeding them would only pin dead data.
        """
        with self._lock:
            for instance_str, services in persisted.services_by_instance.items():
                if not _is_valid_instance_key(instance_str):
                    continue
                self._services_by_instance[instance_str] = dict(services)
            for instance_str, label_to_name in persisted.label_to_name_by_instance.items():
                if not _is_valid_instance_key(instance_str):
                    continue
                self._label_to_name_by_instance[instance_str] = dict(label_to_name)

    def _persist_snapshot(self, snapshot: PersistedServiceMap) -> None:
        """Persist the service-map cache.

        Called (outside ``self._lock``) at every point that mutates the
        per-instance services or label maps, so a later run can seed from the
        full maps.
        """
        if self.service_map_cache is not None:
            self.service_map_cache.persist(snapshot)

    def update_ssh_info(self, instance_key: AgentInstanceKey, ssh_info: RemoteSSHInfo) -> None:
        """Set or replace the SSH info for a single agent instance."""
        with self._lock:
            self._ssh_by_instance[str(instance_key)] = ssh_info

    def resolve_instance_for_coordinate(self, coordinate: str) -> AgentInstanceKey | None:
        """Map an origin coordinate to the agent instance that serves it.

        The canonical coordinate is the agent id itself; a legacy
        ``host-<hex>`` coordinate (URLs minted before the agent keying)
        resolves through :meth:`resolve_agent_for_host`.
        """
        if coordinate.startswith("agent-"):
            with self._lock:
                candidates = sorted(
                    instance_str
                    for instance_str in self._known_agent_instances
                    if AgentInstanceKey(instance_str).agent_id == coordinate
                )
            if not candidates:
                return None
            # The same agent id can exist on two hosts during a migration
            # window; the pick is deterministic (smallest instance key).
            return AgentInstanceKey(candidates[0])
        return self.resolve_agent_for_host(coordinate)

    def resolve_agent_for_host(self, host_id_str: str) -> AgentInstanceKey | None:
        """Map a legacy Host-header ``host-<hex>`` coordinate to the agent instance that serves it.

        The instance key carries the host coordinate, so membership alone
        answers this. When several known agents share the host (possible in
        general, though the plugin's CEL filters usually reduce to one per
        host), the choice is deterministic: the lexicographically-smallest
        instance key wins. Agent ids are unique per host, so this ordering is
        equivalent to ordering by agent id within the host.
        """
        with self._lock:
            candidates = sorted(
                instance_str
                for instance_str in self._known_agent_instances
                if AgentInstanceKey(instance_str).host_id == host_id_str
            )
        if not candidates:
            return None
        return AgentInstanceKey(candidates[0])

    def list_known_agent_instances(self) -> tuple[AgentInstanceKey, ...]:
        """All currently-known agent instance keys (sorted for stable ordering)."""
        with self._lock:
            return tuple(AgentInstanceKey(instance_str) for instance_str in sorted(self._known_agent_instances))

    def has_completed_initial_discovery(self) -> bool:
        with self._lock:
            return self._initial_discovery_done

    def get_ssh_info(self, instance_key: AgentInstanceKey) -> RemoteSSHInfo | None:
        with self._lock:
            return self._ssh_by_instance.get(str(instance_key))

    def resolve(self, instance_key: AgentInstanceKey, service_name: str | None = None) -> ProxyTarget | None:
        """Resolve ``(instance_key, service_name)`` to a backend ``ProxyTarget``, or None if unroutable.

        ``service_name`` is the label parsed from a service origin
        (``<name>.agent-<hex>.localhost``); ``None`` means the bare workspace
        origin, which maps to the configured strategy (the shell service in
        service mode, the fixed port in manual mode). A named service is
        looked up directly in the instance's registered service map, so any
        registered service is reachable at its own origin.
        """
        with self._lock:
            instance_str = str(instance_key)
            if instance_str not in self._known_agent_instances:
                return None
            ssh_info = self._ssh_by_instance.get(instance_str)
            services = self._services_by_instance.get(instance_str, {})

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
