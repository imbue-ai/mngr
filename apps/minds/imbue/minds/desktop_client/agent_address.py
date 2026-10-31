from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredAgent


def build_agent_address(agent_id: AgentId, backend_resolver: BackendResolverInterface) -> str:
    """Render ``agent_id`` as ``AGENT@HOST_ID.PROVIDER`` when discovery places it on exactly one host, else as the bare id.

    ``mngr`` reads only the named host for such an address, instead of discovering
    every host of every provider, so a single-agent command does not wait on the
    user's other machines. The bare id stands whenever the host is not known:
    before the first discovery, and while the id is on several hosts at once
    (mid-migration), where pinning one would pick an instance on the caller's
    behalf. A pin that has gone stale fails as "not found" rather than searching
    elsewhere.
    """
    return agent_address_for_instance(agent_id, backend_resolver.find_sole_agent_instance(agent_id))


def agent_address_for_instance(agent_id: AgentId, instance: DiscoveredAgent | None) -> str:
    """:func:`build_agent_address` for a caller that already read the placement and acts on it too."""
    if instance is None:
        return str(agent_id)
    return f"{agent_id}@{instance.host_id}.{instance.provider_name}"
