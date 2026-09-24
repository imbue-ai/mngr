import pytest

from imbue.mngr.api.conftest import PinnedLookupHarness
from imbue.mngr.api.conftest import PinnedLookupHarnessFactory
from imbue.mngr.api.find import find_all_agents
from imbue.mngr.api.find import find_one_agent_and_agents_by_host
from imbue.mngr.errors import AgentIdNotFoundError
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import allow_warnings


def _pinned(agent_id: AgentId, host_id: HostId, provider_name: ProviderInstanceName) -> AgentAddress:
    return AgentAddress(agent=agent_id, host=HostAddress(host=host_id, provider=provider_name))


def _find_by_addresses(harness: PinnedLookupHarness, addresses: list[AgentAddress]) -> list[tuple[AgentId, HostId]]:
    matches = find_all_agents(addresses=addresses, filter_all=False, target_state=None, mngr_ctx=harness.mngr_ctx)
    return [(match.agent_id, match.host_id) for match in matches]


def _provider_wide_calls(harness: PinnedLookupHarness) -> list[str]:
    return harness.first_provider.provider_wide_discovery_calls + harness.second_provider.provider_wide_discovery_calls


def test_a_pinned_address_reads_only_its_host_and_runs_no_discovery(
    pinned_lookup_harness: PinnedLookupHarness,
) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("pinned-agent"))

    found = _find_by_addresses(harness, [_pinned(agent_id, harness.first_host_id, harness.first_provider.name)])

    assert found == [(agent_id, harness.first_host_id)]
    assert _provider_wide_calls(harness) == []
    assert harness.first_provider.pinned_read_host_ids == [harness.first_host_id]
    assert harness.first_provider.agent_read_host_ids == [harness.first_host_id]
    assert harness.second_provider.pinned_read_host_ids == []
    assert harness.second_provider.agent_read_host_ids == []


def test_a_bare_agent_id_still_runs_discovery(pinned_lookup_harness: PinnedLookupHarness) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("bare-agent"))

    found = _find_by_addresses(harness, [AgentAddress(agent=agent_id)])

    assert found == [(agent_id, harness.first_host_id)]
    assert "discover_hosts_and_agents" in harness.first_provider.provider_wide_discovery_calls
    assert harness.first_provider.pinned_read_host_ids == []


def test_a_stale_pin_fails_as_not_found_naming_the_pinned_host_without_looking_elsewhere(
    pinned_lookup_harness: PinnedLookupHarness,
) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.second_provider.add_agent(harness.second_host_id, agent_id, AgentName("moved-agent"))

    with allow_warnings(match="Agent lookup failed"):
        with pytest.raises(AgentIdNotFoundError, match=f"on pinned host\\(s\\) {harness.first_host_id}"):
            _find_by_addresses(harness, [_pinned(agent_id, harness.first_host_id, harness.first_provider.name)])

    assert _provider_wide_calls(harness) == []
    assert harness.second_provider.pinned_read_host_ids == []


def test_a_pin_to_an_unknown_host_fails_as_not_found_without_discovery(
    pinned_lookup_harness: PinnedLookupHarness,
) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("some-agent"))
    unknown_host_id = HostId.generate()

    with allow_warnings(match="Agent lookup failed"):
        with pytest.raises(AgentIdNotFoundError, match=f"on pinned host\\(s\\) {unknown_host_id}"):
            _find_by_addresses(harness, [_pinned(agent_id, unknown_host_id, harness.first_provider.name)])

    assert _provider_wide_calls(harness) == []
    assert harness.first_provider.pinned_read_host_ids == [unknown_host_id]


def test_a_pin_to_a_host_discovery_leaves_out_fails_as_not_found(
    make_pinned_lookup_harness: PinnedLookupHarnessFactory,
) -> None:
    harness = make_pinned_lookup_harness.build(first_host_stop_reason=HostState.DESTROYED)
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("destroyed-host-agent"))

    with allow_warnings(match="Agent lookup failed"):
        with pytest.raises(AgentIdNotFoundError):
            _find_by_addresses(harness, [_pinned(agent_id, harness.first_host_id, harness.first_provider.name)])

    assert _provider_wide_calls(harness) == []


def test_a_pin_to_a_disabled_provider_fails_as_not_found_without_asking_it(
    make_pinned_lookup_harness: PinnedLookupHarnessFactory,
) -> None:
    harness = make_pinned_lookup_harness.build(is_second_provider_enabled=False)
    agent_id = AgentId.generate()
    harness.second_provider.add_agent(harness.second_host_id, agent_id, AgentName("disabled-agent"))

    with allow_warnings(match="Agent lookup failed"):
        with pytest.raises(AgentIdNotFoundError):
            _find_by_addresses(harness, [_pinned(agent_id, harness.second_host_id, harness.second_provider.name)])

    assert harness.second_provider.pinned_read_host_ids == []
    assert _provider_wide_calls(harness) == []


def test_a_pinned_duplicate_id_acts_on_the_pinned_instance_only(pinned_lookup_harness: PinnedLookupHarness) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("migrating-agent"))
    harness.second_provider.add_agent(harness.second_host_id, agent_id, AgentName("migrating-agent"))

    pinned_found = _find_by_addresses(harness, [_pinned(agent_id, harness.first_host_id, harness.first_provider.name)])
    bare_found = _find_by_addresses(harness, [AgentAddress(agent=agent_id)])

    assert pinned_found == [(agent_id, harness.first_host_id)]
    assert sorted(bare_found) == sorted([(agent_id, harness.first_host_id), (agent_id, harness.second_host_id)])


def test_mixing_a_pinned_and_a_bare_address_runs_discovery(pinned_lookup_harness: PinnedLookupHarness) -> None:
    harness = pinned_lookup_harness
    pinned_agent_id = AgentId.generate()
    bare_agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, pinned_agent_id, AgentName("pinned-agent"))
    harness.second_provider.add_agent(harness.second_host_id, bare_agent_id, AgentName("bare-agent"))

    found = _find_by_addresses(
        harness,
        [
            _pinned(pinned_agent_id, harness.first_host_id, harness.first_provider.name),
            AgentAddress(agent=bare_agent_id),
        ],
    )

    assert sorted(found) == sorted([(pinned_agent_id, harness.first_host_id), (bare_agent_id, harness.second_host_id)])
    assert "discover_hosts_and_agents" in harness.first_provider.provider_wide_discovery_calls
    assert harness.first_provider.pinned_read_host_ids == []


def test_a_pin_by_host_name_runs_discovery(pinned_lookup_harness: PinnedLookupHarness) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("named-host-agent"))
    address = AgentAddress(
        agent=agent_id, host=HostAddress(host=HostName("pinned-first-host"), provider=harness.first_provider.name)
    )

    found = _find_by_addresses(harness, [address])

    assert found == [(agent_id, harness.first_host_id)]
    assert "discover_hosts_and_agents" in harness.first_provider.provider_wide_discovery_calls
    assert harness.first_provider.pinned_read_host_ids == []


def test_full_discovery_mode_still_takes_the_pinned_path(
    make_pinned_lookup_harness: PinnedLookupHarnessFactory,
) -> None:
    harness = make_pinned_lookup_harness.build(is_full_discovery=True)
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("safe-mode-agent"))

    found = _find_by_addresses(harness, [_pinned(agent_id, harness.first_host_id, harness.first_provider.name)])

    assert found == [(agent_id, harness.first_host_id)]
    assert _provider_wide_calls(harness) == []


def test_a_single_target_lookup_by_pinned_address_runs_no_discovery(
    pinned_lookup_harness: PinnedLookupHarness,
) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, agent_id, AgentName("single-target-agent"))

    host_ref, agent_ref, agents_by_host = find_one_agent_and_agents_by_host(
        _pinned(agent_id, harness.first_host_id, harness.first_provider.name), harness.mngr_ctx
    )

    assert (agent_ref.agent_id, host_ref.host_id) == (agent_id, harness.first_host_id)
    assert host_ref.host_state == HostState.STOPPED
    assert [discovered_host.host_id for discovered_host in agents_by_host] == [harness.first_host_id]
    assert _provider_wide_calls(harness) == []


def test_a_single_target_stale_pin_fails_as_not_found_naming_the_pinned_host(
    pinned_lookup_harness: PinnedLookupHarness,
) -> None:
    harness = pinned_lookup_harness
    agent_id = AgentId.generate()
    harness.first_provider.add_agent(harness.first_host_id, AgentId.generate(), AgentName("other-agent"))
    harness.second_provider.add_agent(harness.second_host_id, agent_id, AgentName("moved-agent"))

    with allow_warnings(match="Agent lookup failed"):
        with pytest.raises(AgentIdNotFoundError, match=f"on pinned host\\(s\\) {harness.first_host_id}"):
            find_one_agent_and_agents_by_host(
                _pinned(agent_id, harness.first_host_id, harness.first_provider.name), harness.mngr_ctx
            )

    assert _provider_wide_calls(harness) == []
