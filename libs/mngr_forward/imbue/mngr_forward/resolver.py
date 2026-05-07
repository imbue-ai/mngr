"""Resolves ``<agent-id>.localhost`` requests to a backend ``ProxyTarget``.

Holds three pieces of state, all updated externally:

- The configured forwarding strategy: either ``ForwardServiceStrategy`` (look
  up a named service URL per agent) or ``ForwardPortStrategy`` (forward to a
  fixed remote port on the agent's host).
- ``services_by_agent``: per-agent service-name → URL, populated from the
  ``mngr event`` stream's ``services`` source.
- ``ssh_by_agent``: per-agent SSH info, populated from the ``mngr observe``
  stream's ``HOST_SSH_INFO`` events; absent for local agents.

The single public method ``resolve(agent_id)`` returns ``None`` when the
agent is unknown, the requested service URL is not yet discovered, or the
agent has no SSH info but the strategy requires one.
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
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo


class ForwardResolver(MutableModel):
    """Maps an agent ID to its current backend ``ProxyTarget``."""

    strategy: ForwardStrategy = Field(
        frozen=True,
        description="Either ForwardServiceStrategy or ForwardPortStrategy; chosen at CLI parse time",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _services_by_agent: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)
    _ssh_by_agent: dict[str, RemoteSSHInfo] = PrivateAttr(default_factory=dict)
    _known_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _initial_discovery_done: bool = PrivateAttr(default=False)

    def update_known_agents(self, agent_ids: tuple[AgentId, ...]) -> None:
        """Replace the set of known agents. Drops services / SSH info for removed agents."""
        with self._lock:
            new_set = {str(aid) for aid in agent_ids}
            removed = self._known_agent_ids - new_set
            for aid_str in removed:
                self._services_by_agent.pop(aid_str, None)
                self._ssh_by_agent.pop(aid_str, None)
            self._known_agent_ids = new_set
            self._initial_discovery_done = True

    def add_known_agent(self, agent_id: AgentId) -> None:
        """Mark a single agent as known (incremental discovery)."""
        with self._lock:
            self._known_agent_ids.add(str(agent_id))
            self._initial_discovery_done = True

    def remove_known_agent(self, agent_id: AgentId) -> None:
        """Mark a single agent as no longer known (incremental destruction)."""
        with self._lock:
            aid_str = str(agent_id)
            self._known_agent_ids.discard(aid_str)
            self._services_by_agent.pop(aid_str, None)
            self._ssh_by_agent.pop(aid_str, None)

    def update_services(self, agent_id: AgentId, services: dict[str, str]) -> None:
        """Replace the known services for a single agent."""
        with self._lock:
            self._services_by_agent[str(agent_id)] = dict(services)

    def update_ssh_info(self, agent_id: AgentId, ssh_info: RemoteSSHInfo) -> None:
        """Set or replace the SSH info for a single agent."""
        with self._lock:
            self._ssh_by_agent[str(agent_id)] = ssh_info

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

    def resolve(self, agent_id: AgentId) -> ProxyTarget | None:
        """Resolve ``agent_id`` to a backend ``ProxyTarget``, or None if unroutable."""
        with self._lock:
            aid_str = str(agent_id)
            if aid_str not in self._known_agent_ids:
                return None
            ssh_info = self._ssh_by_agent.get(aid_str)
            services = self._services_by_agent.get(aid_str, {})

        match self.strategy:
            case ForwardServiceStrategy(service_name=service_name):
                url = services.get(service_name)
                if url is None:
                    return None
                return ProxyTarget(url=BackendUrl(url), ssh_info=ssh_info)
            case ForwardPortStrategy(remote_port=remote_port):
                # Manual mode: target ``127.0.0.1:<remote_port>`` on the agent's
                # host. Local agents reach this directly; remote agents go via
                # an SSH ``direct-tcpip`` tunnel.
                url = f"http://127.0.0.1:{remote_port}"
                return ProxyTarget(url=BackendUrl(url), ssh_info=ssh_info)
            case _ as unreachable:  # pragma: no cover
                assert_never(unreachable)
                raise SwitchError(f"Unknown forwarding strategy: {unreachable}")
