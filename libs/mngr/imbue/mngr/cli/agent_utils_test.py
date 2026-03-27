import pytest

from imbue.mngr.cli.agent_utils import _host_matches_filter
from imbue.mngr.cli.agent_utils import filter_agents_by_host
from imbue.mngr.cli.agent_utils import parse_agent_spec
from imbue.mngr.cli.agent_utils import select_agent_interactively_with_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName


def _make_discovered_host(
    provider: str = "local",
    host_id: HostId | None = None,
    host_name: str = "test-host",
) -> DiscoveredHost:
    """Create a DiscoveredHost for testing."""
    if host_id is None:
        host_id = HostId.generate()
    return DiscoveredHost(
        provider_name=ProviderInstanceName(provider),
        host_id=host_id,
        host_name=HostName(host_name),
    )


def _make_discovered_agent(
    agent_id: AgentId,
    agent_name: str = "test-name",
    host_id: HostId | None = None,
    provider: str = "local",
) -> DiscoveredAgent:
    """Create a DiscoveredAgent for testing."""
    if host_id is None:
        host_id = HostId.generate()
    return DiscoveredAgent(
        agent_id=agent_id,
        agent_name=AgentName(agent_name),
        host_id=host_id,
        provider_name=ProviderInstanceName(provider),
    )


# =============================================================================
# _host_matches_filter tests
# =============================================================================


def test_host_matches_filter_by_host_id() -> None:
    """Test that _host_matches_filter matches by HostId."""
    host_id = HostId.generate()
    host_ref = _make_discovered_host(host_id=host_id, host_name="my-host")

    assert _host_matches_filter(host_ref, str(host_id)) is True
    assert _host_matches_filter(host_ref, str(HostId.generate())) is False


def test_host_matches_filter_by_host_name() -> None:
    """Test that _host_matches_filter matches by HostName."""
    host_ref = _make_discovered_host(host_name="my-host")

    assert _host_matches_filter(host_ref, "my-host") is True
    assert _host_matches_filter(host_ref, "other-host") is False


def test_host_matches_filter_prefers_id_over_name() -> None:
    """Test that if the filter looks like an ID, it checks ID first."""
    host_id = HostId.generate()
    host_ref = _make_discovered_host(host_id=host_id, host_name=str(host_id))

    # When using the actual ID, it should match
    assert _host_matches_filter(host_ref, str(host_id)) is True


# =============================================================================
# filter_agents_by_host tests
# =============================================================================


def test_filter_agents_by_host_filters_by_name() -> None:
    """Test that filter_agents_by_host keeps only matching hosts."""
    host_ref1 = _make_discovered_host(host_name="host-1")
    host_ref2 = _make_discovered_host(host_name="host-2")
    agent_ref = _make_discovered_agent(agent_id=AgentId.generate())
    agents_by_host = {host_ref1: [agent_ref], host_ref2: []}

    filtered = filter_agents_by_host(agents_by_host, "host-1")

    assert len(filtered) == 1
    assert host_ref1 in filtered


def test_filter_agents_by_host_raises_when_no_match() -> None:
    """Test that filter_agents_by_host raises UserInputError when no hosts match."""
    host_ref = _make_discovered_host(host_name="host-1")
    agents_by_host = {host_ref: []}

    with pytest.raises(UserInputError, match="No host found matching"):
        filter_agents_by_host(agents_by_host, "nonexistent-host")


# =============================================================================
# select_agent_interactively_with_host tests
# =============================================================================


def test_select_agent_interactively_raises_when_no_agents(
    temp_mngr_ctx: MngrContext,
) -> None:
    """With a fresh context (no hosts or agents), raises UserInputError."""
    with pytest.raises(UserInputError, match="No agents found"):
        select_agent_interactively_with_host(temp_mngr_ctx)


# =============================================================================
# parse_agent_spec tests
# =============================================================================


def test_parse_agent_spec_returns_none_when_spec_is_none() -> None:
    agent_id, subpath = parse_agent_spec(spec=None, explicit_agent=None, spec_name="Target")

    assert agent_id is None
    assert subpath is None


def test_parse_agent_spec_returns_default_subpath_when_spec_is_none() -> None:
    agent_id, subpath = parse_agent_spec(spec=None, explicit_agent=None, spec_name="Target", default_subpath="sub/dir")

    assert agent_id is None
    assert subpath == "sub/dir"


def test_parse_agent_spec_parses_agent_name_only() -> None:
    agent_id, subpath = parse_agent_spec(spec="my-agent", explicit_agent=None, spec_name="Target")

    assert agent_id == "my-agent"
    assert subpath is None


def test_parse_agent_spec_parses_agent_colon_path() -> None:
    agent_id, subpath = parse_agent_spec(spec="my-agent:src/dir", explicit_agent=None, spec_name="Target")

    assert agent_id == "my-agent"
    assert subpath == "src/dir"


def test_parse_agent_spec_agent_colon_path_uses_matching_default_subpath() -> None:
    agent_id, subpath = parse_agent_spec(
        spec="my-agent:src/dir", explicit_agent=None, spec_name="Target", default_subpath="src/dir"
    )

    assert agent_id == "my-agent"
    assert subpath == "src/dir"


def test_parse_agent_spec_raises_on_conflicting_subpath_and_default() -> None:
    with pytest.raises(UserInputError, match="Cannot specify both a subpath in target"):
        parse_agent_spec(spec="my-agent:src/dir", explicit_agent=None, spec_name="Target", default_subpath="old/path")


def test_parse_agent_spec_raises_on_bare_absolute_path() -> None:
    with pytest.raises(UserInputError, match="Target must include an agent specification"):
        parse_agent_spec(spec="/some/path", explicit_agent=None, spec_name="Target")


def test_parse_agent_spec_raises_on_bare_relative_path() -> None:
    with pytest.raises(UserInputError, match="Source must include an agent specification"):
        parse_agent_spec(spec="./local-dir", explicit_agent=None, spec_name="Source")


def test_parse_agent_spec_raises_on_bare_home_path() -> None:
    with pytest.raises(UserInputError, match="Target must include an agent specification"):
        parse_agent_spec(spec="~/my-dir", explicit_agent=None, spec_name="Target")


def test_parse_agent_spec_raises_on_bare_parent_path() -> None:
    with pytest.raises(UserInputError, match="Source must include an agent specification"):
        parse_agent_spec(spec="../my-dir", explicit_agent=None, spec_name="Source")


def test_parse_agent_spec_explicit_agent_with_no_spec() -> None:
    agent_id, subpath = parse_agent_spec(spec=None, explicit_agent="override-agent", spec_name="Target")

    assert agent_id == "override-agent"
    assert subpath is None


def test_parse_agent_spec_explicit_agent_matches_spec() -> None:
    agent_id, subpath = parse_agent_spec(spec="my-agent:path", explicit_agent="my-agent", spec_name="Target")

    assert agent_id == "my-agent"
    assert subpath == "path"


def test_parse_agent_spec_raises_on_conflicting_explicit_agent() -> None:
    with pytest.raises(UserInputError, match="Cannot specify both --target and --target-agent"):
        parse_agent_spec(spec="agent-a", explicit_agent="agent-b", spec_name="Target")


def test_parse_agent_spec_raises_on_conflicting_source_agent() -> None:
    with pytest.raises(UserInputError, match="Cannot specify both --source and --source-agent"):
        parse_agent_spec(spec="agent-a:path", explicit_agent="agent-b", spec_name="Source")
