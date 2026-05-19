"""Tests for agent address resolution and filtering utilities."""

from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.address_parsers import parse_agent_address
from imbue.mngr.api.address_parsers import parse_agent_or_host_address
from imbue.mngr.api.address_parsers import parse_host_address
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import _address_matches_agent_match
from imbue.mngr.api.find import _post_filter_matches_by_addresses
from imbue.mngr.cli.stop import stop
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName

# =============================================================================
# parse_agent_address tests
# =============================================================================


def test_parse_agent_address_plain_name() -> None:
    """A plain name parses to an agent-only address."""
    addr = parse_agent_address("my-agent")

    assert addr.agent == AgentName("my-agent")
    assert addr.host is None


def test_parse_agent_address_with_host() -> None:
    """NAME@HOST extracts the name and sets host in the address."""
    addr = parse_agent_address("my-agent@myhost")

    assert addr.agent == AgentName("my-agent")
    assert addr.host == HostAddress(host=HostName("myhost"))


def test_parse_agent_address_with_host_and_provider() -> None:
    """NAME@HOST.PROVIDER extracts name and sets host+provider."""
    addr = parse_agent_address("my-agent@myhost.modal")

    assert addr.agent == AgentName("my-agent")
    assert addr.host == HostAddress(host=HostName("myhost"), provider=ProviderInstanceName("modal"))


def test_parse_agent_address_rejects_bare_provider_qualifier() -> None:
    """``NAME@.PROVIDER`` is not a valid agent address: HostAddress requires a host."""
    with pytest.raises(UserInputError):
        parse_agent_address("my-agent@.modal")


def test_parse_agent_address_rejects_dotted_host_name() -> None:
    """A bare 'myhost.docker' is not a valid agent name; reject as user input error."""
    with pytest.raises(UserInputError):
        parse_agent_address("myhost.docker")


def test_parse_agent_address_rejects_ip_address() -> None:
    """An IP-address-shaped string is not a valid agent name."""
    with pytest.raises(UserInputError):
        parse_agent_address("192.168.1.1")


def test_parse_agent_address_rejects_empty_string() -> None:
    """The empty string is not a valid agent address."""
    with pytest.raises(UserInputError):
        parse_agent_address("")


def test_parse_agent_address_rejects_at_only() -> None:
    """A bare ``@HOST`` form lacks an agent name and is rejected."""
    with pytest.raises(UserInputError):
        parse_agent_address("@myhost")


# =============================================================================
# parse_host_address: leading-@ tolerance
# =============================================================================


def test_parse_host_address_accepts_leading_at() -> None:
    """``@HOST`` parses the same as ``HOST`` for host-only contexts."""
    assert parse_host_address("@myhost") == HostAddress(host=HostName("myhost"))
    assert parse_host_address("@myhost.modal") == HostAddress(
        host=HostName("myhost"), provider=ProviderInstanceName("modal")
    )


def test_parse_host_address_rejects_at_only() -> None:
    """``@`` alone has no host component."""
    with pytest.raises(UserInputError):
        parse_host_address("@")


# =============================================================================
# parse_agent_or_host_address tests
# =============================================================================


def test_parse_agent_or_host_address_bare_name_is_agent() -> None:
    """A bare name parses as agent first."""
    addr = parse_agent_or_host_address("my-agent")
    assert addr == AgentAddress(agent=AgentName("my-agent"))


def test_parse_agent_or_host_address_at_prefix_is_host() -> None:
    """A leading ``@`` forces host parsing."""
    addr = parse_agent_or_host_address("@my-host")
    assert addr == HostAddress(host=HostName("my-host"))


def test_parse_agent_or_host_address_at_prefix_with_provider_is_host() -> None:
    """``@HOST.PROVIDER`` parses as host with provider."""
    addr = parse_agent_or_host_address("@my-host.modal")
    assert addr == HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal"))


def test_parse_agent_or_host_address_host_id_is_host() -> None:
    """A bare HostId is treated as a host even without the ``@`` prefix."""
    host_id = HostId.generate()
    addr = parse_agent_or_host_address(str(host_id))
    assert addr == HostAddress(host=host_id)


def test_parse_agent_or_host_address_agent_id_is_agent() -> None:
    """A bare AgentId is treated as an agent."""
    agent_id = AgentId.generate()
    addr = parse_agent_or_host_address(str(agent_id))
    assert addr == AgentAddress(agent=agent_id)


def test_parse_agent_or_host_address_host_dot_provider_is_host() -> None:
    """``HOST.PROVIDER`` (no @, contains dot) falls through to host parsing."""
    addr = parse_agent_or_host_address("myhost.modal")
    assert addr == HostAddress(host=HostName("myhost"), provider=ProviderInstanceName("modal"))


def test_parse_agent_or_host_address_agent_at_host_is_agent() -> None:
    """``AGENT@HOST`` parses as agent with host qualifier."""
    addr = parse_agent_or_host_address("my-agent@my-host")
    assert addr == AgentAddress(agent=AgentName("my-agent"), host=HostAddress(host=HostName("my-host")))


def test_parse_agent_or_host_address_agent_at_host_dot_provider_is_agent() -> None:
    """``AGENT@HOST.PROVIDER`` parses as agent with full host."""
    addr = parse_agent_or_host_address("my-agent@my-host.modal")
    assert addr == AgentAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal")),
    )


# =============================================================================
# HostAddress.matches tests
# =============================================================================


def _make_host(name: str = "myhost", provider: str = "local") -> DiscoveredHost:
    return DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName(name),
        provider_name=ProviderInstanceName(provider),
    )


def _host_address_for(host: DiscoveredHost) -> HostAddress:
    return HostAddress(host=host.host_name, provider=host.provider_name)


def test_host_address_matches_by_name() -> None:
    """A constraint with only a host name matches hosts with that name regardless of provider."""
    constraint = HostAddress(host=HostName("myhost"))

    assert constraint.matches(_host_address_for(_make_host("myhost"))) is True
    assert constraint.matches(_host_address_for(_make_host("otherhost"))) is False


def test_host_address_matches_by_name_and_provider() -> None:
    """A constraint with both host name and provider requires both to match."""
    constraint = HostAddress(host=HostName("myhost"), provider=ProviderInstanceName("modal"))

    assert constraint.matches(_host_address_for(_make_host("myhost", "modal"))) is True
    assert constraint.matches(_host_address_for(_make_host("myhost", "docker"))) is False
    assert constraint.matches(_host_address_for(_make_host("other", "modal"))) is False


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
    address = AgentAddress(agent=AgentName("a"))
    assert _address_matches_agent_match(address, _make_match()) is True


def test_address_matches_agent_match_by_host_name() -> None:
    """An address with host name filters by host name."""
    address = AgentAddress(agent=AgentName("a"), host=HostAddress(host=HostName("myhost")))

    assert _address_matches_agent_match(address, _make_match(host_name="myhost")) is True
    assert _address_matches_agent_match(address, _make_match(host_name="other")) is False


# =============================================================================
# _post_filter_matches_by_addresses tests
# =============================================================================


def test_post_filter_no_host_constraints_passes_all_through() -> None:
    """Plain identifiers (no host) return all matches unchanged."""
    matches = [_make_match("agent1", "host1", "local"), _make_match("agent2", "host2", "modal")]
    addresses = [parse_agent_address("agent1"), parse_agent_address("agent2")]

    result = _post_filter_matches_by_addresses(addresses, matches)

    assert len(result) == 2


def test_post_filter_by_host_name() -> None:
    """An address with host name filters to only that host's agents."""
    match_host1 = _make_match("my-agent", "host1", "local")
    match_host2 = _make_match("my-agent", "host2", "local")
    matches = [match_host1, match_host2]
    addresses = [parse_agent_address("my-agent@host1")]

    result = _post_filter_matches_by_addresses(addresses, matches)

    assert len(result) == 1
    assert result[0].host_name == HostName("host1")


def test_post_filter_by_host_and_provider() -> None:
    """An address with both host and provider requires both to match."""
    match_right = _make_match("my-agent", "host1", "modal")
    match_wrong_host = _make_match("my-agent", "host2", "modal")
    match_wrong_provider = _make_match("my-agent", "host1", "local")
    matches = [match_right, match_wrong_host, match_wrong_provider]
    addresses = [parse_agent_address("my-agent@host1.modal")]

    result = _post_filter_matches_by_addresses(addresses, matches)

    assert len(result) == 1
    assert result[0].host_name == HostName("host1")
    assert result[0].provider_name == ProviderInstanceName("modal")


def test_post_filter_mixed_constrained_and_unconstrained() -> None:
    """Unconstrained identifiers pass through while constrained ones filter."""
    match_a_host1 = _make_match("agent-a", "host1", "local")
    match_a_host2 = _make_match("agent-a", "host2", "modal")
    match_b = _make_match("agent-b", "host3", "local")
    matches = [match_a_host1, match_a_host2, match_b]
    addresses = [parse_agent_address("agent-a@host1"), parse_agent_address("agent-b")]

    result = _post_filter_matches_by_addresses(addresses, matches)

    # agent-a filtered to host1 only, agent-b passes through
    assert len(result) == 2
    result_names_and_hosts = [(str(m.agent_name), str(m.host_name)) for m in result]
    assert ("agent-a", "host1") in result_names_and_hosts
    assert ("agent-b", "host3") in result_names_and_hosts


def test_post_filter_raises_when_constrained_identifier_has_no_match() -> None:
    """Raises AgentNotFoundError if a host-constrained identifier matches nothing."""
    match_wrong_host = _make_match("my-agent", "host2", "local")
    matches = [match_wrong_host]
    addresses = [parse_agent_address("my-agent@host1")]

    with pytest.raises(AgentNotFoundError, match="my-agent@host1"):
        _post_filter_matches_by_addresses(addresses, matches)


def test_post_filter_empty_matches_with_no_constraints() -> None:
    """Empty matches with no constraints returns empty list."""
    result = _post_filter_matches_by_addresses([], [])

    assert result == []


# =============================================================================
# CLI integration: address syntax accepted by commands using the shared code path
# =============================================================================


def test_stop_accepts_address_syntax(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Commands using the shared find_all_agents accept address syntax.

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
