from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.mngr.primitives import AgentId


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
    instance = backend_resolver.find_sole_agent_instance(agent_id)
    if instance is None:
        return str(agent_id)
    return f"{agent_id}@{instance.host_id}.{instance.provider_name}"
