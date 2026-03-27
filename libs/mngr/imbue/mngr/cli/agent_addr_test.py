"""Tests for agent address parsing and resolution utilities."""

from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.cli.agent_addr import AgentAddress
from imbue.mngr.cli.agent_addr import _address_matches_agent_match
from imbue.mngr.cli.agent_addr import _address_matches_host
from imbue.mngr.cli.agent_addr import _post_filter_matches_by_addresses
from imbue.mngr.cli.agent_addr import filter_agents_by_host_constraint
from imbue.mngr.cli.agent_addr import parse_identifier_as_address
from imbue.mngr.cli.stop import stop
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName

# =============================================================================
# parse_identifier_as_address tests
# =============================================================================


def test_parse_identifier_plain_name() -> None:
    """A plain name returns the string unchanged and a name-only address."""
    ident, addr = parse_identifier_as_address("my-agent")

    assert ident == "my-agent"
    assert addr.agent_name == AgentName("my-agent")
    assert addr.host_name is None
    assert addr.provider_name is None


def test_parse_identifier_with_host() -> None:
    """NAME@HOST extracts the name and sets host in the address."""
    ident, addr = parse_identifier_as_address("my-agent@myhost")

    assert ident == "my-agent"
    assert addr.agent_name == AgentName("my-agent")
    assert addr.host_name == HostName("myhost")
    assert addr.provider_name is None


def test_parse_identifier_with_host_and_provider() -> None:
    """NAME@HOST.PROVIDER extracts name and sets host+provider."""
    ident, addr = parse_identifier_as_address("my-agent@myhost.modal")

    assert ident == "my-agent"
    assert addr.host_name == HostName("myhost")
    assert addr.provider_name == ProviderInstanceName("modal")


def test_parse_identifier_with_provider_only() -> None:
    """NAME@.PROVIDER extracts name and sets provider."""
    ident, addr = parse_identifier_as_address("my-agent@.modal")

    assert ident == "my-agent"
    assert addr.host_name is None
    assert addr.provider_name == ProviderInstanceName("modal")


# =============================================================================
# _address_matches_host tests
# =============================================================================


def _make_host(name: str = "myhost", provider: str = "local") -> DiscoveredHost:
    return DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName(name),
        provider_name=ProviderInstanceName(provider),
    )


def test_address_matches_host_no_constraints() -> None:
    """An address with no host component matches any host."""
    address = AgentAddress()
    host = _make_host()

    assert _address_matches_host(address, host) is True


def test_address_matches_host_by_name() -> None:
    """An address with host_name matches hosts with that name."""
    address = AgentAddress(host_name=HostName("myhost"))

    assert _address_matches_host(address, _make_host("myhost")) is True
    assert _address_matches_host(address, _make_host("otherhost")) is False


def test_address_matches_host_by_provider() -> None:
    """An address with provider_name matches hosts with that provider."""
    address = AgentAddress(provider_name=ProviderInstanceName("modal"))

    assert _address_matches_host(address, _make_host(provider="modal")) is True
    assert _address_matches_host(address, _make_host(provider="docker")) is False


def test_address_matches_host_by_name_and_provider() -> None:
    """An address with both host_name and provider_name requires both to match."""
    address = AgentAddress(host_name=HostName("myhost"), provider_name=ProviderInstanceName("modal"))

    assert _address_matches_host(address, _make_host("myhost", "modal")) is True
    assert _address_matches_host(address, _make_host("myhost", "docker")) is False
    assert _address_matches_host(address, _make_host("other", "modal")) is False


# =============================================================================
# _address_matches_agent_match tests
# =============================================================================


def _make_match(
    name: str = "my-agent",
    host_name: str = "myhost",
    provider: str = "local",
) -> AgentMatch:
    return AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName(name),
        host_id=HostId.generate(),
        host_name=HostName(host_name),
        provider_name=ProviderInstanceName(provider),
    )


def test_address_matches_agent_match_no_constraints() -> None:
    """An address with no host component matches any agent match."""
    address = AgentAddress()
    assert _address_matches_agent_match(address, _make_match()) is True


def test_address_matches_agent_match_by_host_name() -> None:
    """An address with host_name filters by host_name."""
    address = AgentAddress(host_name=HostName("myhost"))

    assert _address_matches_agent_match(address, _make_match(host_name="myhost")) is True
    assert _address_matches_agent_match(address, _make_match(host_name="other")) is False


def test_address_matches_agent_match_by_provider() -> None:
    """An address with provider_name filters by provider."""
    address = AgentAddress(provider_name=ProviderInstanceName("modal"))

    assert _address_matches_agent_match(address, _make_match(provider="modal")) is True
    assert _address_matches_agent_match(address, _make_match(provider="local")) is False


# =============================================================================
# filter_agents_by_host_constraint tests
# =============================================================================


def test_filter_agents_no_constraint() -> None:
    """When address has no host component, return all agents."""
    host = _make_host("h1", "local")
    agents: list[DiscoveredAgent] = []
    agents_by_host = {host: agents}

    result = filter_agents_by_host_constraint(agents_by_host, AgentAddress())
    assert len(result) == 1


def test_filter_agents_by_host_name() -> None:
    """Filter keeps only hosts matching the address host_name."""
    host1 = _make_host("h1", "local")
    host2 = _make_host("h2", "local")
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {host1: [], host2: []}

    address = AgentAddress(host_name=HostName("h1"))
    result = filter_agents_by_host_constraint(agents_by_host, address)
    assert len(result) == 1
    assert host1 in result


def test_filter_agents_by_provider() -> None:
    """Filter keeps only hosts matching the address provider_name."""
    host1 = _make_host("h1", "local")
    host2 = _make_host("h2", "modal")
    agents_by_host: dict[DiscoveredHost, list[DiscoveredAgent]] = {host1: [], host2: []}

    address = AgentAddress(provider_name=ProviderInstanceName("modal"))
    result = filter_agents_by_host_constraint(agents_by_host, address)
    assert len(result) == 1
    assert host2 in result


# =============================================================================
# _post_filter_matches_by_addresses tests
# =============================================================================


def test_post_filter_no_host_constraints_passes_all_through() -> None:
    """Plain identifiers (no @) return all matches unchanged."""
    matches = [_make_match("agent1", "host1", "local"), _make_match("agent2", "host2", "modal")]
    parsed = [parse_identifier_as_address("agent1"), parse_identifier_as_address("agent2")]

    result = _post_filter_matches_by_addresses(["agent1", "agent2"], parsed, matches)

    assert len(result) == 2


def test_post_filter_by_host_name() -> None:
    """An address with host name filters to only that host's agents."""
    match_host1 = _make_match("my-agent", "host1", "local")
    match_host2 = _make_match("my-agent", "host2", "local")
    matches = [match_host1, match_host2]
    parsed = [parse_identifier_as_address("my-agent@host1")]

    result = _post_filter_matches_by_addresses(["my-agent@host1"], parsed, matches)

    assert len(result) == 1
    assert result[0].host_name == HostName("host1")


def test_post_filter_by_provider() -> None:
    """An address with provider filters to only that provider's agents."""
    match_local = _make_match("my-agent", "host1", "local")
    match_modal = _make_match("my-agent", "host2", "modal")
    matches = [match_local, match_modal]
    parsed = [parse_identifier_as_address("my-agent@.modal")]

    result = _post_filter_matches_by_addresses(["my-agent@.modal"], parsed, matches)

    assert len(result) == 1
    assert result[0].provider_name == ProviderInstanceName("modal")


def test_post_filter_by_host_and_provider() -> None:
    """An address with both host and provider requires both to match."""
    match_right = _make_match("my-agent", "host1", "modal")
    match_wrong_host = _make_match("my-agent", "host2", "modal")
    match_wrong_provider = _make_match("my-agent", "host1", "local")
    matches = [match_right, match_wrong_host, match_wrong_provider]
    parsed = [parse_identifier_as_address("my-agent@host1.modal")]

    result = _post_filter_matches_by_addresses(["my-agent@host1.modal"], parsed, matches)

    assert len(result) == 1
    assert result[0].host_name == HostName("host1")
    assert result[0].provider_name == ProviderInstanceName("modal")


def test_post_filter_mixed_constrained_and_unconstrained() -> None:
    """Unconstrained identifiers pass through while constrained ones filter."""
    match_a_host1 = _make_match("agent-a", "host1", "local")
    match_a_host2 = _make_match("agent-a", "host2", "modal")
    match_b = _make_match("agent-b", "host3", "local")
    matches = [match_a_host1, match_a_host2, match_b]
    parsed = [parse_identifier_as_address("agent-a@host1"), parse_identifier_as_address("agent-b")]

    result = _post_filter_matches_by_addresses(["agent-a@host1", "agent-b"], parsed, matches)

    # agent-a filtered to host1 only, agent-b passes through
    assert len(result) == 2
    result_names_and_hosts = [(str(m.agent_name), str(m.host_name)) for m in result]
    assert ("agent-a", "host1") in result_names_and_hosts
    assert ("agent-b", "host3") in result_names_and_hosts


def test_post_filter_raises_when_constrained_identifier_has_no_match() -> None:
    """Raises AgentNotFoundError if a host-constrained identifier matches nothing."""
    match_wrong_host = _make_match("my-agent", "host2", "local")
    matches = [match_wrong_host]
    parsed = [parse_identifier_as_address("my-agent@host1")]

    with pytest.raises(AgentNotFoundError, match="my-agent@host1"):
        _post_filter_matches_by_addresses(["my-agent@host1"], parsed, matches)


def test_post_filter_empty_matches_with_no_constraints() -> None:
    """Empty matches with no constraints returns empty list."""
    result = _post_filter_matches_by_addresses([], [], [])

    assert result == []


# =============================================================================
# CLI integration: address syntax accepted by commands using the shared code path
# =============================================================================


def test_stop_accepts_address_syntax(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Commands using the shared find_agents_by_addresses accept address syntax.

    Using 'stop' as a representative: passing NAME@HOST should not crash with a
    parsing error. It will fail with 'agent not found' (expected) rather than a
    syntax error, proving the address is parsed correctly.
    """
    result = cli_runner.invoke(
        stop,
        ["nonexistent@somehost.local"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    # The address should be parsed without error. The command fails because no
    # agent named "nonexistent" exists, not because the address syntax is invalid.
    assert result.exit_code != 0
    assert "nonexistent" in result.output


def test_stop_accepts_plain_name_unchanged(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Plain agent names (no @) still work as before with the address-aware code path."""
    result = cli_runner.invoke(
        stop,
        ["nonexistent-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "nonexistent-agent" in result.output
