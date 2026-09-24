from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import CHAT_ID_LABEL
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.discover import pinned_hosts_for_addresses
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName

_PROVIDER = ProviderInstanceName("imbue_cloud_gabriel-imbue-com")


def _discovered(agent_id: AgentId, host_id: HostId, labels: dict[str, str] | None = None) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("workspace"),
        provider_name=_PROVIDER,
        certified_data={"labels": labels} if labels is not None else {},
    )


def _resolver_with(*agents: DiscoveredAgent) -> MngrCliBackendResolver:
    resolver = MngrCliBackendResolver()
    resolver.update_agents(
        ParsedAgentsResult(
            agent_ids=tuple(agent.agent_id for agent in agents),
            discovered_agents=agents,
        )
    )
    return resolver


def test_an_agent_on_one_known_host_is_addressed_so_mngr_reads_only_that_host() -> None:
    """Asserted through mngr's own parser: the property that matters is that mngr takes the pinned path."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()

    address = build_agent_address(agent_id, _resolver_with(_discovered(agent_id, host_id)))

    parsed = parse_agent_address(address)
    assert parsed.agent == agent_id
    pinned = pinned_hosts_for_addresses([parsed])
    assert pinned is not None
    assert [(pinned_host.host_id, pinned_host.provider_name) for pinned_host in pinned] == [(host_id, _PROVIDER)]


def test_an_agent_discovery_has_not_seen_is_addressed_by_bare_id() -> None:
    agent_id = AgentId.generate()

    assert build_agent_address(agent_id, MngrCliBackendResolver()) == str(agent_id)


def test_an_agent_id_on_two_hosts_is_addressed_by_bare_id() -> None:
    """Mid-migration an id names an instance per host, and picking one is not the helper's call."""
    agent_id = AgentId.generate()
    resolver = _resolver_with(_discovered(agent_id, HostId.generate()), _discovered(agent_id, HostId.generate()))

    assert build_agent_address(agent_id, resolver) == str(agent_id)


def test_a_chat_id_is_never_pinned_to_its_member_agents_host() -> None:
    """The resolver maps a chat id to a member for display, but mngr knows no agent by the chat's id."""
    chat_id = AgentId.generate()
    member = _discovered(AgentId.generate(), HostId.generate(), labels={CHAT_ID_LABEL: str(chat_id)})

    assert build_agent_address(chat_id, _resolver_with(member)) == str(chat_id)


def test_an_agent_whose_host_is_mid_stop_keeps_its_pinned_address() -> None:
    """A stop or start is exactly when discovery drops the host, and exactly when the pinned address is wanted."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    resolver = _resolver_with(_discovered(agent_id, host_id))
    resolver.mark_host_lifecycle_transition_started(host_id)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(), discovered_agents=()))

    assert build_agent_address(agent_id, resolver) == f"{agent_id}@{host_id}.{_PROVIDER}"
