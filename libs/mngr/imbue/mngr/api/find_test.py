from pathlib import Path

import pytest

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import ParsedSourceLocation
from imbue.mngr.api.find import determine_resolved_path
from imbue.mngr.api.find import find_agents_by_identifiers_or_state
from imbue.mngr.api.find import find_all_matching_agents
from imbue.mngr.api.find import find_all_matching_hosts
from imbue.mngr.api.find import get_host_from_list_by_id
from imbue.mngr.api.find import get_unique_host_from_list_by_name
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.find import parse_source_string
from imbue.mngr.api.find import resolve_agent_reference
from imbue.mngr.api.find import resolve_host_reference
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
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderInstanceName


def test_parse_source_string_with_agent_only() -> None:
    parsed = parse_source_string(
        source="my-agent",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host=None,
        path=None,
    )


def test_parse_source_string_with_agent_and_host() -> None:
    parsed = parse_source_string(
        source="my-agent.my-host",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host="my-host",
        path=None,
    )


def test_parse_source_string_with_agent_host_and_path() -> None:
    parsed = parse_source_string(
        source="my-agent.my-host:/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host="my-host",
        path="/path/to/dir",
    )


def test_parse_source_string_with_host_and_path() -> None:
    parsed = parse_source_string(
        source="my-host:/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host="my-host",
        path="/path/to/dir",
    )


def test_parse_source_string_with_absolute_path() -> None:
    parsed = parse_source_string(
        source="/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path="/path/to/dir",
    )


def test_parse_source_string_with_relative_path() -> None:
    parsed = parse_source_string(
        source="./path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path="./path/to/dir",
    )


def test_parse_source_string_with_home_path() -> None:
    parsed = parse_source_string(
        source="~/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path="~/path/to/dir",
    )


def test_parse_source_string_with_parent_path() -> None:
    parsed = parse_source_string(
        source="../path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path="../path/to/dir",
    )


def test_parse_source_string_with_individual_parameters() -> None:
    parsed = parse_source_string(
        source=None,
        source_agent="my-agent",
        source_host="my-host",
        source_path="/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host="my-host",
        path="/path/to/dir",
    )


def test_parse_source_string_with_all_none() -> None:
    parsed = parse_source_string(
        source=None,
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path=None,
    )


def test_parse_source_string_raises_when_both_source_and_individual_params() -> None:
    with pytest.raises(UserInputError, match="Specify either --source or the individual source parameters"):
        parse_source_string(
            source="my-agent",
            source_agent="another-agent",
            source_host=None,
            source_path=None,
        )


def test_resolve_host_reference_with_none() -> None:
    result = resolve_host_reference(
        host_identifier=None,
        all_hosts=[],
    )

    assert result is None


def test_resolve_host_reference_by_id() -> None:
    host_id = HostId.generate()
    host_ref = DiscoveredHost(
        host_id=host_id,
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )

    result = resolve_host_reference(
        host_identifier=str(host_id),
        all_hosts=[host_ref],
    )

    assert result == host_ref


def test_resolve_host_reference_by_name() -> None:
    host_ref = DiscoveredHost(
        host_id=HostId.generate(),
        host_name=HostName("test-host"),
        provider_name=ProviderInstanceName("local"),
    )

    result = resolve_host_reference(
        host_identifier="test-host",
        all_hosts=[host_ref],
    )

    assert result == host_ref


def test_resolve_host_reference_raises_when_not_found() -> None:
    with pytest.raises(UserInputError, match="Could not find host with ID or name: nonexistent"):
        resolve_host_reference(
            host_identifier="nonexistent",
            all_hosts=[],
        )


def test_resolve_host_reference_raises_when_multiple_hosts_with_same_name() -> None:
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
        resolve_host_reference(
            host_identifier="test-host",
            all_hosts=[host_ref1, host_ref2],
        )


def test_resolve_agent_reference_with_none() -> None:
    result = resolve_agent_reference(
        agent_identifier=None,
        resolved_host=None,
        agents_by_host={},
    )

    assert result is None


def test_resolve_agent_reference_by_id() -> None:
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

    result = resolve_agent_reference(
        agent_identifier=str(agent_id),
        resolved_host=None,
        agents_by_host={host_ref: [agent_ref]},
    )

    assert result == (host_ref, agent_ref)


def test_resolve_agent_reference_by_name() -> None:
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

    result = resolve_agent_reference(
        agent_identifier="test-agent",
        resolved_host=None,
        agents_by_host={host_ref: [agent_ref]},
    )

    assert result == (host_ref, agent_ref)


def test_resolve_agent_reference_with_resolved_host_filters_by_host() -> None:
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

    result = resolve_agent_reference(
        agent_identifier="test-agent",
        resolved_host=host_ref1,
        agents_by_host={
            host_ref1: [agent_ref1],
            host_ref2: [agent_ref2],
        },
    )

    assert result == (host_ref1, agent_ref1)


def test_resolve_agent_reference_raises_when_not_found() -> None:
    with pytest.raises(UserInputError, match="Could not find agent with ID or name: nonexistent"):
        resolve_agent_reference(
            agent_identifier="nonexistent",
            resolved_host=None,
            agents_by_host={},
        )


def test_resolve_agent_reference_raises_when_multiple_agents_match() -> None:
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

    with pytest.raises(UserInputError, match="Multiple agents found with ID or name: test-agent"):
        resolve_agent_reference(
            agent_identifier="test-agent",
            resolved_host=None,
            agents_by_host={
                host_ref1: [agent_ref1],
                host_ref2: [agent_ref2],
            },
        )


def test_parse_source_string_with_colons_in_path() -> None:
    parsed = parse_source_string(
        source="my-host:/path/with:colons:in:it.txt",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host="my-host",
        path="/path/with:colons:in:it.txt",
    )


def test_parse_source_string_with_agent_host_and_colons_in_path() -> None:
    parsed = parse_source_string(
        source="agent.host:/weird:path:file.txt",
    )

    assert parsed == ParsedSourceLocation(
        agent="agent",
        host="host",
        path="/weird:path:file.txt",
    )


def test_parse_source_string_with_empty_path_after_colon() -> None:
    parsed = parse_source_string(
        source="my-host:",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host="my-host",
        path="",
    )


def test_parse_source_string_with_url_as_path() -> None:
    parsed = parse_source_string(
        source="my-agent:http://example.com/path",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host="my-agent",
        path="http://example.com/path",
    )


def test_parse_source_string_with_agent_host_provider() -> None:
    parsed = parse_source_string(
        source="my-agent.my-host.docker",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host="my-host.docker",
        path=None,
    )


def test_parse_source_string_with_agent_host_provider_and_path() -> None:
    parsed = parse_source_string(
        source="my-agent.my-host.modal:/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-agent",
        host="my-host.modal",
        path="/path/to/dir",
    )


def test_parse_source_string_with_host_provider_and_path_ambiguity() -> None:
    parsed = parse_source_string(
        source="my-host.docker:/path/to/dir",
    )

    assert parsed == ParsedSourceLocation(
        agent="my-host",
        host="docker",
        path="/path/to/dir",
    )


def test_parse_source_string_with_windows_drive_letter_ambiguity() -> None:
    parsed = parse_source_string(
        source="C:/Windows/path",
    )

    assert parsed == ParsedSourceLocation(
        agent=None,
        host="C",
        path="/Windows/path",
    )


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
        parsed_path="/explicit/path",
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


def test_determine_resolved_path_prefers_parsed_path_over_agent_work_dir() -> None:
    """determine_resolved_path should prefer parsed path even when agent work dir available."""
    agent_ref = DiscoveredAgent(
        host_id=HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName("test"),
        provider_name=ProviderInstanceName("local"),
    )
    result = determine_resolved_path(
        parsed_path="/explicit/path",
        resolved_agent=agent_ref,
        agent_work_dir_if_available=Path("/agent/work/dir"),
    )
    assert result == Path("/explicit/path")


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


def test_parse_source_string_with_empty_prefix_before_colon() -> None:
    """parse_source_string should handle :path format (empty prefix before colon)."""
    parsed = parse_source_string(source=":/path/to/dir")
    assert parsed == ParsedSourceLocation(
        agent=None,
        host=None,
        path="/path/to/dir",
    )


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
# find_agents_by_identifiers_or_state Tests
# =============================================================================


def test_find_agents_by_identifiers_or_state_no_agents_returns_empty(
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agents_by_identifiers_or_state should return empty list when no agents exist and filter_all is True."""
    result = find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=True,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )
    assert result == []


def test_find_agents_by_identifiers_or_state_no_identifiers_and_not_all(
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agents_by_identifiers_or_state should return empty list when no identifiers and filter_all is False."""
    result = find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )
    assert result == []


def test_find_agents_by_identifiers_or_state_raises_on_unknown_identifier(
    temp_mngr_ctx: MngrContext,
) -> None:
    """find_agents_by_identifiers_or_state should raise AgentNotFoundError for unrecognized identifiers."""
    with pytest.raises(AgentNotFoundError, match="No agent"):
        find_agents_by_identifiers_or_state(
            agent_identifiers=["nonexistent-agent-xyz"],
            filter_all=False,
            target_state=None,
            mngr_ctx=temp_mngr_ctx,
        )


@pytest.mark.tmux
def test_find_agents_by_identifiers_or_state_finds_by_name(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_by_identifiers_or_state should find an agent by its name."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-by-name-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847310"),
        ),
    )

    results = find_agents_by_identifiers_or_state(
        agent_identifiers=["find-by-name-test"],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    assert len(results) == 1
    assert results[0].agent_name == AgentName("find-by-name-test")


@pytest.mark.tmux
def test_find_agents_by_identifiers_or_state_finds_by_id(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_by_identifiers_or_state should find an agent by its ID."""
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-by-id-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847311"),
        ),
    )

    agent_id_str = str(agent.id)
    results = find_agents_by_identifiers_or_state(
        agent_identifiers=[agent_id_str],
        filter_all=False,
        target_state=None,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    assert len(results) == 1
    assert str(results[0].agent_id) == agent_id_str


@pytest.mark.tmux
def test_find_agents_by_identifiers_or_state_filter_all_returns_all(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_by_identifiers_or_state with filter_all=True, target_state=None returns all agents."""
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

    results = find_agents_by_identifiers_or_state(
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
def test_find_agents_by_identifiers_or_state_filter_by_stopped_state(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    local_host: Host,
) -> None:
    """find_agents_by_identifiers_or_state with target_state=STOPPED should only return stopped agents."""
    # Create an agent but don't start it (so it's stopped)
    agent = local_host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("find-stopped-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847314"),
        ),
    )

    results = find_agents_by_identifiers_or_state(
        agent_identifiers=[],
        filter_all=True,
        target_state=AgentLifecycleState.STOPPED,
        mngr_ctx=temp_mngr_ctx,
    )

    local_host.destroy_agent(agent)

    found_names = {str(r.agent_name) for r in results}
    assert "find-stopped-test" in found_names


# --- find_all_matching_hosts ---


def test_find_all_matching_hosts_by_name() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("my-host"), provider_name=ProviderInstanceName("local")
    )
    result = find_all_matching_hosts("my-host", [host])
    assert result == [host]


def test_find_all_matching_hosts_by_id() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("my-host"), provider_name=ProviderInstanceName("local")
    )
    result = find_all_matching_hosts(str(host.host_id), [host])
    assert result == [host]


def test_find_all_matching_hosts_no_match() -> None:
    host = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("other"), provider_name=ProviderInstanceName("local")
    )
    assert find_all_matching_hosts("nonexistent", [host]) == []


def test_find_all_matching_hosts_multiple() -> None:
    host1 = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("shared"), provider_name=ProviderInstanceName("local")
    )
    host2 = DiscoveredHost(
        host_id=HostId.generate(), host_name=HostName("shared"), provider_name=ProviderInstanceName("local")
    )
    result = find_all_matching_hosts("shared", [host1, host2])
    assert len(result) == 2


# --- find_all_matching_agents ---


def test_find_all_matching_agents_by_name() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("my-agent"),
        provider_name=ProviderInstanceName("local"),
    )
    result = find_all_matching_agents("my-agent", {host: [agent]})
    assert len(result) == 1
    assert result[0] == (host, agent)


def test_find_all_matching_agents_by_id() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("a"),
        provider_name=ProviderInstanceName("local"),
    )
    result = find_all_matching_agents(str(agent.agent_id), {host: [agent]})
    assert len(result) == 1


def test_find_all_matching_agents_no_match() -> None:
    host_id = HostId.generate()
    host = DiscoveredHost(host_id=host_id, host_name=HostName("h"), provider_name=ProviderInstanceName("local"))
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("other"),
        provider_name=ProviderInstanceName("local"),
    )
    assert find_all_matching_agents("nonexistent", {host: [agent]}) == []


def test_find_all_matching_agents_multiple() -> None:
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
    result = find_all_matching_agents("shared", {host1: [agent1], host2: [agent2]})
    assert len(result) == 2


def test_find_all_matching_agents_filtered_by_host() -> None:
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
    result = find_all_matching_agents("shared", {host1: [agent1], host2: [agent2]}, resolved_host=host1)
    assert len(result) == 1
    assert result[0] == (host1, agent1)
