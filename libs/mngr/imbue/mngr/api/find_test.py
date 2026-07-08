from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.address_parsers import parse_host_location_address
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import _filter_all_agents
from imbue.mngr.api.find import _find_agents_by_identifiers_or_state
from imbue.mngr.api.find import determine_resolved_path
from imbue.mngr.api.find import ensure_agent_started
from imbue.mngr.api.find import filter_all_hosts
from imbue.mngr.api.find import filter_one_agent
from imbue.mngr.api.find import filter_one_host
from imbue.mngr.api.find import get_host_from_list_by_id
from imbue.mngr.api.find import get_unique_host_from_list_by_name
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostLocationAddress
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_parse_host_location_address_with_agent_only() -> None:
    parsed = parse_host_location_address("my-agent")

    assert parsed == HostLocationAddress(agent=AgentName("my-agent"))


def test_parse_host_location_address_with_agent_and_host() -> None:
    parsed = parse_host_location_address("my-agent@my-host")

    assert parsed == HostLocationAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host")),
    )


def test_parse_host_location_address_with_agent_host_and_provider() -> None:
    parsed = parse_host_location_address("my-agent@my-host.modal")

    assert parsed == HostLocationAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal")),
    )


def test_parse_host_location_address_with_agent_host_and_path() -> None:
    parsed = parse_host_location_address("my-agent@my-host:/path/to/dir")

    assert parsed == HostLocationAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host")),
        path=Path("/path/to/dir"),
    )


def test_parse_host_location_address_with_host_and_path() -> None:
    parsed = parse_host_location_address("@my-host:/path/to/dir")

    assert parsed == HostLocationAddress(
        host=HostAddress(host=HostName("my-host")),
        path=Path("/path/to/dir"),
    )


def test_parse_host_location_address_with_absolute_path() -> None:
    parsed = parse_host_location_address("/path/to/dir")

    assert parsed == HostLocationAddress(path=Path("/path/to/dir"))


def test_parse_host_location_address_with_relative_path() -> None:
    parsed = parse_host_location_address("./path/to/dir")

    assert parsed == HostLocationAddress(path=Path("./path/to/dir"))


def test_parse_host_location_address_with_home_path() -> None:
    parsed = parse_host_location_address("~/path/to/dir")

    assert parsed == HostLocationAddress(path=Path("~/path/to/dir"))


def test_parse_host_location_address_with_parent_path() -> None:
    parsed = parse_host_location_address("../path/to/dir")

    assert parsed == HostLocationAddress(path=Path("../path/to/dir"))


def test_parse_host_location_address_bare_name_is_agent_not_path() -> None:
    """A bare name like 'foo' refers to agent 'foo', not directory 'foo'."""
    parsed = parse_host_location_address("foo")

    assert parsed == HostLocationAddress(agent=AgentName("foo"))


def test_parse_host_location_address_colon_prefix_is_local_path() -> None:
    """:dirname is how to specify a relative local directory."""
    parsed = parse_host_location_address(":my-dir")

    assert parsed == HostLocationAddress(path=Path("my-dir"))


def test_filter_one_host_by_id() -> None:
    host_id = HostId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )

    result = filter_one_host(
        address=HostAddress(host=host_id),
        all_hosts=[host_ref],
    )

    assert result == host_ref


def test_filter_one_host_by_name() -> None:
    host_ref = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )

    result = filter_one_host(
        address=HostAddress(host=HostName("test-host")),
        all_hosts=[host_ref],
    )

    assert result == host_ref


def test_filter_one_host_raises_when_not_found() -> None:
    with pytest.raises(UserInputError, match="Could not find host with ID or name: nonexistent"):
        filter_one_host(
            address=HostAddress(host=HostName("nonexistent")),
            all_hosts=[],
        )


def test_filter_one_host_disambiguates_with_host_provider_form() -> None:
    """filter_one_host should pick the right host when 'host.provider' is given."""
    host_modal = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("m1"),
        provider_name=ProviderInstanceName("modal"),
    )
    host_docker = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("m1"),
        provider_name=ProviderInstanceName("docker"),
    )

    result = filter_one_host(
        address=HostAddress(host=HostName("m1"), provider=ProviderInstanceName("modal")),
        all_hosts=[host_modal, host_docker],
    )

    assert result == host_modal


def test_filter_one_host_raises_when_multiple_hosts_with_same_name() -> None:
    host_ref1 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )
    host_ref2 = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("docker"),
    )

    with pytest.raises(UserInputError, match="Multiple hosts found with name: test-host"):
        filter_one_host(
            address=HostAddress(host=HostName("test-host")),
            all_hosts=[host_ref1, host_ref2],
        )


def test_filter_one_agent_by_id() -> None:
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )

    result = filter_one_agent(
        agent=agent_id,
        resolved_host=None,
        agents_by_host={host_ref: [agent_ref]},
    )

    assert result == (host_ref, agent_ref)


def test_filter_one_agent_by_name() -> None:
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )

    result = filter_one_agent(
        agent=AgentName("test-agent"),
        resolved_host=None,
        agents_by_host={host_ref: [agent_ref]},
    )

    assert result == (host_ref, agent_ref)


def test_filter_one_agent_with_resolved_host_filters_by_host() -> None:
    host_id1 = HostId.generate()
    host_id2 = HostId.generate()
    agent_id1 = AgentId.generate()
    agent_id2 = AgentId.generate()

    host_ref1 = DiscoveredHost(
        host_id=host_id1,
        host_name=HostName("host1"),
        provider_name=ProviderInstanceName("local"),
    )
    host_ref2 = DiscoveredHost(
        host_id=host_id2,
        host_name=HostName("host2"),
        provider_name=ProviderInstanceName("local"),
    )

    agent_ref1 = DiscoveredAgent(
        host_id=host_id1,
        agent_id=agent_id1,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref2 = DiscoveredAgent(
        host_id=host_id2,
        agent_id=agent_id2,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )

    result = filter_one_agent(
        agent=AgentName("test-agent"),
        resolved_host=host_ref1,
        agents_by_host={
            host_ref1: [agent_ref1],
            host_ref2: [agent_ref2],
        },
    )

    assert result == (host_ref1, agent_ref1)


def test_filter_one_agent_raises_when_not_found() -> None:
    with pytest.raises(UserInputError, match="Could not find agent with ID or name: nonexistent"):
        filter_one_agent(
            agent=AgentName("nonexistent"),
            resolved_host=None,
            agents_by_host={},
        )


def test_filter_one_agent_raises_agent_not_found_for_unknown_id() -> None:
    """An unknown AgentId raises AgentNotFoundError (not UserInputError).

    The distinction lets callers detect "the specific agent you named no
    longer exists" separately from "your search term didn't match anything".
    """
    with pytest.raises(AgentNotFoundError):
        filter_one_agent(
            agent=AgentId.generate(),
            resolved_host=None,
            agents_by_host={},
        )


def test_filter_one_agent_raises_when_multiple_agents_match() -> None:
    host_id1 = HostId.generate()
    host_id2 = HostId.generate()
    agent_id1 = AgentId.generate()
    agent_id2 = AgentId.generate()

    host_ref1 = DiscoveredHost(
        host_id=host_id1,
        host_name=HostName("host1"),
        provider_name=ProviderInstanceName("local"),
    )
    host_ref2 = DiscoveredHost(
        host_id=host_id2,
        host_name=HostName("host2"),
        provider_name=ProviderInstanceName("local"),
    )

    agent_ref1 = DiscoveredAgent(
        host_id=host_id1,
        agent_id=agent_id1,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    agent_ref2 = DiscoveredAgent(
        host_id=host_id2,
        agent_id=agent_id2,
        agent_name=AgentName("test-agent"),
        provider_name=ProviderInstanceName("local"),
    )

    with pytest.raises(UserInputError, match="Multiple agents found with name 'test-agent'"):
        filter_one_agent(
            agent=AgentName("test-agent"),
            resolved_host=None,
            agents_by_host={
                host_ref1: [agent_ref1],
                host_ref2: [agent_ref2],
            },
        )


def test_parse_host_location_address_with_colons_in_path() -> None:
    parsed = parse_host_location_address("@my-host:/path/with:colons:in:it.txt")

    assert parsed == HostLocationAddress(
        host=HostAddress(host=HostName("my-host")),
        path=Path("/path/with:colons:in:it.txt"),
    )


def test_parse_host_location_address_with_agent_host_and_colons_in_path() -> None:
    parsed = parse_host_location_address("agent@host:/weird:path:file.txt")

    assert parsed == HostLocationAddress(
        agent=AgentName("agent"),
        host=HostAddress(host=HostName("host")),
        path=Path("/weird:path:file.txt"),
    )


def test_parse_host_location_address_with_empty_path_after_colon() -> None:
    parsed = parse_host_location_address("@my-host:")

    assert parsed == HostLocationAddress(host=HostAddress(host=HostName("my-host")))


def test_parse_host_location_address_with_agent_and_path() -> None:
    parsed = parse_host_location_address("my-agent:http://example.com/path")

    assert parsed == HostLocationAddress(agent=AgentName("my-agent"), path=Path("http://example.com/path"))


def test_parse_host_location_address_with_agent_host_provider() -> None:
    parsed = parse_host_location_address("my-agent@my-host.docker")

    assert parsed == HostLocationAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("docker")),
    )


def test_parse_host_location_address_with_agent_host_provider_and_path() -> None:
    parsed = parse_host_location_address("my-agent@my-host.modal:/path/to/dir")

    assert parsed == HostLocationAddress(
        agent=AgentName("my-agent"),
        host=HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal")),
        path=Path("/path/to/dir"),
    )


def test_parse_host_location_address_with_host_provider_and_path() -> None:
    parsed = parse_host_location_address("@my-host.docker:/path/to/dir")

    assert parsed == HostLocationAddress(
        host=HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("docker")),
        path=Path("/path/to/dir"),
    )


def test_parse_host_location_address_with_agent_colon_path() -> None:
    """Agent name followed by colon and path (no host)."""
    parsed = parse_host_location_address("C:/Windows/path")

    assert parsed == HostLocationAddress(agent=AgentName("C"), path=Path("/Windows/path"))


def test_parse_host_location_address_rejects_multi_dot_host() -> None:
    """parse_host_location_address should reject HOST.PROVIDER strings with more than one dot."""
    with pytest.raises(UserInputError, match="contains more than one dot"):
        parse_host_location_address("@a.b.c:/path")


def test_get_host_from_list_by_id_returns_matching_host() -> None:
    """get_host_from_list_by_id should return matching host."""
    host_id = HostId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    result = get_host_from_list_by_id(host_id, [host_ref])
    assert result == host_ref


def test_get_host_from_list_by_id_returns_none_when_not_found() -> None:
    """get_host_from_list_by_id should return None when not found."""
    result = get_host_from_list_by_id(HostId.generate(), [])
    assert result is None


def test_get_unique_host_from_list_by_name_returns_matching_host() -> None:
    """get_unique_host_from_list_by_name should return matching host."""
    host_name = HostName("test-host")
    host_ref = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=host_name,
        provider_name=ProviderInstanceName("local"),
    )
    result = get_unique_host_from_list_by_name(host_name, [host_ref])
    assert result == host_ref


def test_get_unique_host_from_list_by_name_returns_none_when_empty() -> None:
    """get_unique_host_from_list_by_name should return None for empty list."""
    result = get_unique_host_from_list_by_name(HostName("test"), [])
    assert result is None


def test_determine_resolved_path_uses_parsed_path_when_available() -> None:
    """determine_resolved_path should prefer parsed_path when available."""
    result = determine_resolved_path(
        parsed_path=Path("/explicit/path"),
        resolved_agent=None,
        agent_work_dir_if_available=None,
    )
    assert result == Path("/explicit/path")


def test_determine_resolved_path_uses_agent_work_dir_when_no_parsed_path() -> None:
    """determine_resolved_path should use agent work dir when no parsed path."""
    agent_ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    result = determine_resolved_path(
        parsed_path=None,
        resolved_agent=agent_ref,
        agent_work_dir_if_available=Path("/agent/work/dir"),
    )
    assert result == Path("/agent/work/dir")


def test_determine_resolved_path_keeps_absolute_parsed_path_over_agent_work_dir() -> None:
    """An absolute parsed path is honored verbatim even when an agent work dir is available."""
    agent_ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    result = determine_resolved_path(
        parsed_path=Path("/explicit/path"),
        resolved_agent=agent_ref,
        agent_work_dir_if_available=Path("/agent/work/dir"),
    )
    assert result == Path("/explicit/path")


def test_determine_resolved_path_joins_relative_parsed_path_onto_agent_work_dir() -> None:
    """A relative parsed path next to an agent resolves against that agent's work dir."""
    agent_ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    result = determine_resolved_path(
        parsed_path=Path("runtime/reports"),
        resolved_agent=agent_ref,
        agent_work_dir_if_available=Path("/agent/work/dir"),
    )
    assert result == Path("/agent/work/dir/runtime/reports")


def test_determine_resolved_path_keeps_relative_parsed_path_without_agent() -> None:
    """A relative parsed path with no agent is returned verbatim (resolved by the caller's cwd)."""
    result = determine_resolved_path(
        parsed_path=Path("runtime/reports"),
        resolved_agent=None,
        agent_work_dir_if_available=None,
    )
    assert result == Path("runtime/reports")


def test_determine_resolved_path_raises_when_agent_but_no_work_dir() -> None:
    """determine_resolved_path should raise when agent specified but work dir not found."""
    agent_ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    with pytest.raises(UserInputError, match="Could not find agent"):
        determine_resolved_path(
            parsed_path=None,
            resolved_agent=agent_ref,
            agent_work_dir_if_available=None,
        )


def test_determine_resolved_path_raises_when_no_path_and_no_agent() -> None:
    """determine_resolved_path should raise when neither path nor agent specified."""
    with pytest.raises(UserInputError, match="Must specify a path"):
        determine_resolved_path(
            parsed_path=None,
            resolved_agent=None,
            agent_work_dir_if_available=None,
        )


def test_parse_host_location_address_with_empty_prefix_before_colon() -> None:
    """parse_host_location_address should handle :path format (empty prefix before colon)."""
    parsed = parse_host_location_address(":/path/to/dir")
    assert parsed == HostLocationAddress(path=Path("/path/to/dir"))


# =============================================================================
# AgentMatch Tests
# =============================================================================


def test_agent_match_construction() -> None:
    """AgentMatch should be constructable with required fields."""
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    match = AgentMatch(
        agent_id=agent_id,
        agent_name=AgentName("my-agent"),
        host_id=host_id,
        host_name=HostName("my-host"),
        provider_name=ProviderInstanceName("local"),
    )
    assert match.agent_id == agent_id
    assert match.agent_name == AgentName("my-agent")
    assert match.host_id == host_id
    assert match.host_name == HostName("my-host")
    assert match.provider_name == ProviderInstanceName("local")


# =============================================================================
# group_agents_by_host Tests
# =============================================================================


def test_group_agents_by_host_empty_list() -> None:
    """group_agents_by_host should return empty dict for empty input."""
    result = group_agents_by_host([])
    assert result == {}


def test_group_agents_by_host_single_host() -> None:
    """group_agents_by_host should group agents on the same host."""
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("local")
    match1 = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-1"),
        host_id=host_id,
        host_name=HostName("host"),
        provider_name=provider_name,
    )
    match2 = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-2"),
        host_id=host_id,
        host_name=HostName("host"),
        provider_name=provider_name,
    )

    result = group_agents_by_host([match1, match2])
    key = f"{host_id}:{provider_name}"
    assert key in result
    assert len(result[key]) == 2
    assert result[key][0] == match1
    assert result[key][1] == match2


def test_group_agents_by_host_multiple_hosts() -> None:
    """group_agents_by_host should separate agents from different hosts."""
    host_id_1 = HostId.generate()
    host_id_2 = HostId.generate()
    provider_name = ProviderInstanceName("local")

    match1 = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-1"),
        host_id=host_id_1,
        host_name=HostName("host-1"),
        provider_name=provider_name,
    )
    match2 = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent-2"),
        host_id=host_id_2,
        host_name=HostName("host-2"),
        provider_name=provider_name,
    )

    result = group_agents_by_host([match1, match2])
    assert len(result) == 2

    key1 = f"{host_id_1}:{provider_name}"
    key2 = f"{host_id_2}:{provider_name}"
    assert key1 in result
    assert key2 in result
    assert result[key1] == [match1]
    assert result[key2] == [match2]


def test_group_agents_by_host_key_format() -> None:
    """group_agents_by_host should use '{host_id}:{provider_name}' as key format."""
    host_id = HostId.generate()
    provider_name = ProviderInstanceName("docker")
    match = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("agent"),
        host_id=host_id,
        host_name=HostName("host"),
        provider_name=provider_name,
    )

    result = group_agents_by_host([match])
    expected_key = f"{host_id}:docker"
    assert expected_key in result


# =============================================================================
# _find_agents_by_identifiers_or_state Tests
# =============================================================================


def test__find_agents_by_identifiers_or_state_no_agents_returns_empty(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_find_agents_by_identifiers_or_state should return empty list when no agents exist and filter_all is True."""
    result = _find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )
    assert result == []


def test__find_agents_by_identifiers_or_state_no_identifiers_and_not_all(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_find_agents_by_identifiers_or_state should return empty list when no identifiers and filter_all is False."""
    result = _find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )
    assert result == []


def test__find_agents_by_identifiers_or_state_raises_on_unknown_identifier(
    temp_mngr_ctx: MngrContext,
) -> None:
    """_find_agents_by_identifiers_or_state should raise AgentNotFoundError for unrecognized identifiers."""
    with pytest.raises(AgentNotFoundError, match="No agent"):
        _find_agents_by_identifiers_or_state(
            agent_identifiers=[AgentName("nonexistent-agent-xyz")],
            filter_all=False,
            target_state=None,
            mngr_ctx=temp_mngr_ctx,
        )


@pytest.mark.tmux
def test__find_agents_by_identifiers_or_state_finds_by_name(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """_find_agents_by_identifiers_or_state should find an agent by its name."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-by-name-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847310"),
        ),
    )

    results = _find_agents_by_identifiers_or_state(
        agent_identifiers=[AgentName("find-by-name-test")],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    assert len(results) == 1
    assert results[0].agent_name == AgentName("find-by-name-test")


@pytest.mark.tmux
def test__find_agents_by_identifiers_or_state_finds_by_id(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """_find_agents_by_identifiers_or_state should find an agent by its ID."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-by-id-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847311"),
        ),
    )

    results = _find_agents_by_identifiers_or_state(
        agent_identifiers=[agent.id],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    assert len(results) == 1
    assert results[0].agent_id == agent.id


@pytest.mark.tmux
def test__find_agents_by_identifiers_or_state_filter_all_returns_all(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """_find_agents_by_identifiers_or_state with filter_all=True, target_state=None returns all agents."""
    agent1 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-all-1"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847312"),
        ),
    )
    agent2 = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-all-2"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847313"),
        ),
    )

    results = _find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent1)
    local_host.destroy_agent(agent2)

    found_names = {str(r.agent_name) for r in results}
    assert "find-all-1" in found_names
    assert "find-all-2" in found_names


@pytest.mark.tmux
def test__find_agents_by_identifiers_or_state_filter_by_stopped_state(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """_find_agents_by_identifiers_or_state with target_state=STOPPED should only return stopped agents."""
    # Create an agent but don't start it (so it's stopped)
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-stopped-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847314"),
        ),
    )

    results = _find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=True,
        target_state=AgentLifecycleState.STOPPED,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    found_names = {str(r.agent_name) for r in results}
    assert "find-stopped-test" in found_names


# --- filter_all_hosts ---


def test_filter_all_hosts_by_name() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("my-host"), provider_name=ProviderInstanceName("local")
    )
    result = filter_all_hosts(HostAddress(host=HostName("my-host")), [host])
    assert result == [host]


def test_filter_all_hosts_by_id() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("my-host"), provider_name=ProviderInstanceName("local")
    )
    result = filter_all_hosts(HostAddress(host=host.host_id), [host])
    assert result == [host]


def test_filter_all_hosts_no_match() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("other"), provider_name=ProviderInstanceName("local")
    )
    assert filter_all_hosts(HostAddress(host=HostName("nonexistent")), [host]) == []


def test_filter_all_hosts_multiple() -> None:
    host1 = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("shared"), provider_name=ProviderInstanceName("local")
    )
    host2 = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("shared"), provider_name=ProviderInstanceName("local")
    )
    result = filter_all_hosts(HostAddress(host=HostName("shared")), [host1, host2])
    assert len(result) == 2


def test_filter_all_hosts_by_host_provider_form() -> None:
    """filter_all_hosts should match the 'host.provider' form."""
    host_modal = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("m1"), provider_name=ProviderInstanceName("modal")
    )
    host_docker = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("m1"), provider_name=ProviderInstanceName("docker")
    )
    result = filter_all_hosts(
        HostAddress(host=HostName("m1"), provider=ProviderInstanceName("modal")),
        [host_modal, host_docker],
    )
    assert result == [host_modal]


def test_filter_all_hosts_host_provider_form_no_match() -> None:
    """If the provider suffix does not match any host, return no matches."""
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("m1"), provider_name=ProviderInstanceName("modal")
    )
    assert (
        filter_all_hosts(
            HostAddress(host=HostName("m1"), provider=ProviderInstanceName("docker")),
            [host],
        )
        == []
    )


# --- _filter_all_agents ---


def test__filter_all_agents_by_name() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("my-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    result = _filter_all_agents(AgentName("my-agent"), {host: [agent]})
    assert len(result) == 1
    assert result[0] == (host, agent)


def test__filter_all_agents_by_id() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("a"),
        provider_name=ProviderInstanceName("local"),
    )
    result = _filter_all_agents(agent.agent_id, {host: [agent]})
    assert len(result) == 1


def test__filter_all_agents_no_match() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("other"),
        provider_name=ProviderInstanceName("local"),
    )
    assert _filter_all_agents(AgentName("nonexistent"), {host: [agent]}) == []


def test__filter_all_agents_multiple() -> None:
    host1_id = HostId.generate()
    host2_id = HostId.generate()
    host1 = DiscoveredHost(host_id=host1_id, host_name=HostName("h1"), provider_name=ProviderInstanceName("local"))
    host2 = DiscoveredHost(host_id=host2_id, host_name=HostName("h2"), provider_name=ProviderInstanceName("local"))
    agent1 = DiscoveredAgent(
        host_id=host1_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared"),
        provider_name=ProviderInstanceName("local"),
    )
    agent2 = DiscoveredAgent(
        host_id=host2_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared"),
        provider_name=ProviderInstanceName("local"),
    )
    result = _filter_all_agents(AgentName("shared"), {host1: [agent1], host2: [agent2]})
    assert len(result) == 2


def test__filter_all_agents_filtered_by_host() -> None:
    host1_id = HostId.generate()
    host2_id = HostId.generate()
    host1 = DiscoveredHost(host_id=host1_id, host_name=HostName("h1"), provider_name=ProviderInstanceName("local"))
    host2 = DiscoveredHost(host_id=host2_id, host_name=HostName("h2"), provider_name=ProviderInstanceName("local"))
    agent1 = DiscoveredAgent(
        host_id=host1_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared"),
        provider_name=ProviderInstanceName("local"),
    )
    agent2 = DiscoveredAgent(
        host_id=host2_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("shared"),
        provider_name=ProviderInstanceName("local"),
    )
    result = _filter_all_agents(AgentName("shared"), {host1: [agent1], host2: [agent2]}, resolved_host=host1)
    assert len(result) == 1
    assert result[0] == (host1, agent1)


class _TimeoutCapturingAgent(BaseAgent[AgentTypeConfig]):
    """Test agent that records the timeout passed to wait_for_ready_signal."""

    captured_timeouts: list[float | None] = Field(default_factory=list)

    def wait_for_ready_signal(
        self,
        is_creating: bool,
        start_action: Callable[[], None],
        timeout: float | None = None,
    ) -> None:
        self.captured_timeouts.append(timeout)


@pytest.mark.tmux
def test_ensure_agent_started_uses_per_agent_ready_timeout(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """ensure_agent_started must use the agent's configured ready_timeout_seconds."""
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data={"ready_timeout_seconds": 42.0},
        agent_class=_TimeoutCapturingAgent,
    )
    assert isinstance(agent, _TimeoutCapturingAgent)
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    ensure_agent_started(agent, agent.host, is_start_desired=True)

    assert agent.captured_timeouts == [42.0]


@pytest.mark.tmux
def test_ensure_agent_started_respects_config_when_data_unset(
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
) -> None:
    """ensure_agent_started must fall back to MngrConfig.agent_ready_timeout when data.json has no override."""
    base_ctx = local_provider.mngr_ctx
    new_config = base_ctx.config.model_copy_update(
        to_update(base_ctx.config.field_ref().agent_ready_timeout, 37.5),
    )
    new_ctx = base_ctx.model_copy_update(
        to_update(base_ctx.field_ref().config, new_config),
    )
    local_provider = local_provider.model_copy_update(
        to_update(local_provider.field_ref().mngr_ctx, new_ctx),
    )
    agent = create_test_agent(
        local_provider,
        temp_work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=_TimeoutCapturingAgent,
    )
    assert isinstance(agent, _TimeoutCapturingAgent)
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    ensure_agent_started(agent, agent.host, is_start_desired=True)

    assert agent.captured_timeouts == [37.5]
