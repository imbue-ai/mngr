import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.logging import generate_log_event_id
from imbue.mngr.api.discovery_aggregator import DiscoveryStateAggregator
from imbue.mngr.api.discovery_aggregator import RemovedItemDecision
from imbue.mngr.api.discovery_aggregator import classify_removed_item
from imbue.mngr.api.discovery_aggregator import is_intervening_event
from imbue.mngr.api.discovery_aggregator import parse_event_timestamp
from imbue.mngr.api.discovery_aggregator import should_apply_snapshot_item
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import ProviderDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.discovery_events import make_provider_discovery_snapshot_event
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_BASE_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(at: datetime) -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(at))


def _make_agent(provider: str, name: str, host_id: HostId | None = None) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id or HostId.generate(),
        agent_id=AgentId.generate(),
        agent_name=AgentName(name),
        provider_name=ProviderInstanceName(provider),
        certified_data={},
    )


def _make_host(provider: str, name: str, host_id: HostId | None = None) -> DiscoveredHost:
    return DiscoveredHost(
        host_id=host_id or HostId.generate(),
        host_name=HostName(name),
        provider_name=ProviderInstanceName(provider),
        host_state=HostState.RUNNING,
    )


def _snapshot(
    provider: str,
    agents: tuple[DiscoveredAgent, ...],
    hosts: tuple[DiscoveredHost, ...],
    started_at: datetime = _BASE_TIME,
    finished_at: datetime | None = None,
    error: DiscoveryError | None = None,
    unknown_agent_ids: tuple[AgentId, ...] = (),
    unknown_host_ids: tuple[HostId, ...] = (),
) -> ProviderDiscoverySnapshotEvent:
    return make_provider_discovery_snapshot_event(
        provider_name=ProviderInstanceName(provider),
        agents=agents,
        hosts=hosts,
        discovery_started_at=started_at,
        discovery_finished_at=finished_at or (started_at + timedelta(seconds=1)),
        error=error,
        unknown_agent_ids=unknown_agent_ids,
        unknown_host_ids=unknown_host_ids,
    )


def _agent_discovered(agent: DiscoveredAgent, at: datetime) -> AgentDiscoveryEvent:
    return AgentDiscoveryEvent(
        timestamp=_iso(at),
        event_id=EventId(generate_log_event_id()),
        source=DISCOVERY_EVENT_SOURCE,
        agent=agent,
    )


def _agent_destroyed(agent: DiscoveredAgent, at: datetime) -> AgentDestroyedEvent:
    return AgentDestroyedEvent(
        timestamp=_iso(at),
        event_id=EventId(generate_log_event_id()),
        source=DISCOVERY_EVENT_SOURCE,
        agent_id=agent.agent_id,
        host_id=agent.host_id,
    )


def _host_destroyed(host: DiscoveredHost, agent_ids: tuple[AgentId, ...], at: datetime) -> HostDestroyedEvent:
    return HostDestroyedEvent(
        timestamp=_iso(at),
        event_id=EventId(generate_log_event_id()),
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host.host_id,
        agent_ids=agent_ids,
    )


# === Pure helper tests ===


def test_is_intervening_event_none_is_false() -> None:
    assert is_intervening_event(None, _BASE_TIME) is False


def test_is_intervening_event_before_span_is_false() -> None:
    assert is_intervening_event(_BASE_TIME - timedelta(seconds=1), _BASE_TIME) is False


def test_is_intervening_event_at_or_after_span_start_is_true() -> None:
    assert is_intervening_event(_BASE_TIME, _BASE_TIME) is True
    assert is_intervening_event(_BASE_TIME + timedelta(seconds=1), _BASE_TIME) is True


def test_classify_removed_item_drops_when_healthy_and_no_intervening() -> None:
    assert classify_removed_item(is_provider_errored=False, has_intervening_event=False) is RemovedItemDecision.DROP


def test_classify_removed_item_retains_when_provider_errored() -> None:
    assert classify_removed_item(is_provider_errored=True, has_intervening_event=False) is RemovedItemDecision.RETAIN


def test_classify_removed_item_retains_when_intervening_event() -> None:
    assert classify_removed_item(is_provider_errored=False, has_intervening_event=True) is RemovedItemDecision.RETAIN


def test_should_apply_snapshot_item_false_when_intervening() -> None:
    assert should_apply_snapshot_item(has_intervening_event=True) is False
    assert should_apply_snapshot_item(has_intervening_event=False) is True


def test_parse_event_timestamp_round_trips_to_utc() -> None:
    parsed = parse_event_timestamp(_iso(_BASE_TIME))
    assert parsed == _BASE_TIME


# === Aggregator: basic snapshot application ===


def test_apply_provider_snapshot_adds_agents_and_hosts() -> None:
    aggregator = DiscoveryStateAggregator()
    host = _make_host("docker", "h1")
    agent = _make_agent("docker", "a1", host_id=host.host_id)
    delta = aggregator.apply_event(_snapshot("docker", (agent,), (host,)))

    assert {a.agent_id for a in aggregator.get_agents()} == {agent.agent_id}
    assert {h.host_id for h in aggregator.get_hosts()} == {host.host_id}
    assert delta.added_agent_ids == frozenset({str(agent.agent_id)})
    assert delta.added_host_ids == frozenset({str(host.host_id)})


def test_snapshot_for_one_provider_does_not_drop_another_providers_agents() -> None:
    """The core per-provider guarantee: a provider's snapshot is scoped to its own agents."""
    aggregator = DiscoveryStateAggregator()
    docker_agent = _make_agent("docker", "docker-a")
    modal_agent = _make_agent("modal", "modal-a")
    aggregator.apply_event(_snapshot("docker", (docker_agent,), ()))
    aggregator.apply_event(_snapshot("modal", (modal_agent,), ()))

    # A fresh docker snapshot that no longer lists docker_agent must NOT touch modal_agent.
    aggregator.apply_event(_snapshot("docker", (), ()))

    remaining = {a.agent_id for a in aggregator.get_agents()}
    assert docker_agent.agent_id not in remaining
    assert modal_agent.agent_id in remaining


def test_errored_provider_snapshot_retains_prior_agents_as_unknown() -> None:
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("modal", "stuck-a")
    aggregator.apply_event(_snapshot("modal", (agent,), ()))

    error = DiscoveryError(type_name="ModalAuthError", message="token", provider_name=ProviderInstanceName("modal"))
    aggregator.apply_event(_snapshot("modal", (), (), error=error))

    # The agent is retained (not dropped) and surfaced as unknown.
    assert agent.agent_id in {a.agent_id for a in aggregator.get_agents()}
    assert str(agent.agent_id) in aggregator.get_unknown_agent_ids()
    assert ProviderInstanceName("modal") in aggregator.get_error_by_provider_name()


def test_healthy_provider_snapshot_drops_absent_agent() -> None:
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("docker", "gone-a")
    aggregator.apply_event(_snapshot("docker", (agent,), ()))
    aggregator.apply_event(_snapshot("docker", (), ()))

    assert aggregator.get_agents() == []
    assert str(agent.agent_id) not in aggregator.get_unknown_agent_ids()


def test_unknown_agent_id_in_snapshot_retains_and_marks_unknown() -> None:
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("vps", "slow-a")
    aggregator.apply_event(_snapshot("vps", (agent,), ()))
    # Next poll cannot read this agent in time -> producer marks it unknown and omits it.
    aggregator.apply_event(_snapshot("vps", (), (), unknown_agent_ids=(agent.agent_id,)))

    assert agent.agent_id in {a.agent_id for a in aggregator.get_agents()}
    assert str(agent.agent_id) in aggregator.get_unknown_agent_ids()


def test_error_then_success_clears_error_and_unknown() -> None:
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("modal", "a")
    aggregator.apply_event(_snapshot("modal", (agent,), ()))
    error = DiscoveryError(type_name="X", message="y", provider_name=ProviderInstanceName("modal"))
    aggregator.apply_event(_snapshot("modal", (), (), error=error))
    assert str(agent.agent_id) in aggregator.get_unknown_agent_ids()

    # Provider recovers and re-lists the agent.
    aggregator.apply_event(_snapshot("modal", (agent,), ()))
    assert str(agent.agent_id) not in aggregator.get_unknown_agent_ids()
    assert ProviderInstanceName("modal") not in aggregator.get_error_by_provider_name()


# === The intervening-event-during-span race ===


def test_destroy_during_span_is_not_resurrected_by_in_flight_snapshot() -> None:
    """A destroy that lands during a provider's discovery span must win over the snapshot.

    The snapshot began before the destroy and still lists the agent (it read stale
    state), but the destroy reflects newer truth, so the agent must stay gone.
    """
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("docker", "racing-a")
    # Initial state: the agent exists.
    aggregator.apply_event(_snapshot("docker", (agent,), (), started_at=_BASE_TIME))

    span_start = _BASE_TIME + timedelta(seconds=10)
    # A destroy lands DURING the next poll's span.
    aggregator.apply_event(_agent_destroyed(agent, at=span_start + timedelta(seconds=1)))
    assert agent.agent_id not in {a.agent_id for a in aggregator.get_agents()}

    # The in-flight snapshot (started before the destroy) still lists the agent.
    aggregator.apply_event(
        _snapshot("docker", (agent,), (), started_at=span_start, finished_at=span_start + timedelta(seconds=2))
    )

    # The agent must NOT be resurrected.
    assert agent.agent_id not in {a.agent_id for a in aggregator.get_agents()}


def test_destroy_before_span_is_honored_by_snapshot() -> None:
    """A destroy that predates the span is legitimately reflected by the snapshot (no resurrection issue)."""
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("docker", "old-a")
    aggregator.apply_event(_snapshot("docker", (agent,), (), started_at=_BASE_TIME))

    # Destroy happens, THEN a later snapshot (started after the destroy) omits it.
    destroy_at = _BASE_TIME + timedelta(seconds=5)
    aggregator.apply_event(_agent_destroyed(agent, at=destroy_at))
    aggregator.apply_event(_snapshot("docker", (), (), started_at=destroy_at + timedelta(seconds=5)))

    assert aggregator.get_agents() == []


def test_state_change_during_span_is_not_clobbered_by_in_flight_snapshot() -> None:
    """A newer agent-discovered event during the span must not be overwritten by stale snapshot data."""
    aggregator = DiscoveryStateAggregator()
    host_id = HostId.generate()
    agent_id = AgentId.generate()
    stale_agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("renamed-old"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"work_dir": "/old"},
    )
    fresh_agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName("renamed-new"),
        provider_name=ProviderInstanceName("docker"),
        certified_data={"work_dir": "/new"},
    )
    aggregator.apply_event(_snapshot("docker", (stale_agent,), (), started_at=_BASE_TIME))

    span_start = _BASE_TIME + timedelta(seconds=10)
    # A fresh event lands during the span with new data.
    aggregator.apply_event(_agent_discovered(fresh_agent, at=span_start + timedelta(seconds=1)))
    # The in-flight snapshot carries the stale data.
    aggregator.apply_event(
        _snapshot("docker", (stale_agent,), (), started_at=span_start, finished_at=span_start + timedelta(seconds=2))
    )

    current = aggregator.get_agent_by_id()[str(agent_id)]
    assert current.certified_data["work_dir"] == "/new"


def test_host_destroyed_event_removes_host_and_its_agents() -> None:
    aggregator = DiscoveryStateAggregator()
    host = _make_host("docker", "h1")
    agent = _make_agent("docker", "a1", host_id=host.host_id)
    aggregator.apply_event(_snapshot("docker", (agent,), (host,), started_at=_BASE_TIME))

    delta = aggregator.apply_event(_host_destroyed(host, (agent.agent_id,), at=_BASE_TIME + timedelta(seconds=5)))

    assert aggregator.get_hosts() == []
    assert aggregator.get_agents() == []
    assert delta.removed_host_ids == frozenset({str(host.host_id)})
    assert delta.removed_agent_ids == frozenset({str(agent.agent_id)})


# === Freshness + provider metadata ===


def test_last_event_at_tracks_latest_event() -> None:
    aggregator = DiscoveryStateAggregator()
    agent = _make_agent("docker", "a1")
    aggregator.apply_event(_agent_discovered(agent, at=_BASE_TIME))
    later = _BASE_TIME + timedelta(seconds=30)
    aggregator.apply_event(_agent_discovered(_make_agent("docker", "a2"), at=later))
    assert aggregator.get_last_event_at() == later


def test_last_snapshot_at_is_per_provider() -> None:
    aggregator = DiscoveryStateAggregator()
    docker_finished = _BASE_TIME + timedelta(seconds=1)
    modal_finished = _BASE_TIME + timedelta(seconds=50)
    aggregator.apply_event(_snapshot("docker", (), (), started_at=_BASE_TIME, finished_at=docker_finished))
    aggregator.apply_event(
        _snapshot("modal", (), (), started_at=_BASE_TIME + timedelta(seconds=49), finished_at=modal_finished)
    )

    assert aggregator.get_last_snapshot_at_for_provider(ProviderInstanceName("docker")) == docker_finished
    assert aggregator.get_last_snapshot_at_for_provider(ProviderInstanceName("modal")) == modal_finished
    assert aggregator.get_last_snapshot_at() == modal_finished


def test_snapshot_records_provider_metadata() -> None:
    aggregator = DiscoveryStateAggregator()
    provider = make_discovered_provider(
        ProviderInstanceName("docker"),
        ProviderInstanceConfig(backend=ProviderBackendName("docker"), is_enabled=True),
    )
    event = make_provider_discovery_snapshot_event(
        provider_name=ProviderInstanceName("docker"),
        agents=(),
        hosts=(),
        discovery_started_at=_BASE_TIME,
        discovery_finished_at=_BASE_TIME + timedelta(seconds=1),
        provider=provider,
    )
    aggregator.apply_event(event)
    assert [p.provider_name for p in aggregator.get_providers()] == [ProviderInstanceName("docker")]


# === Event round-trip ===


def test_provider_discovery_snapshot_event_round_trips() -> None:
    host = _make_host("docker", "h1")
    agent = _make_agent("docker", "a1", host_id=host.host_id)
    event = _snapshot("docker", (agent,), (host,), unknown_agent_ids=(AgentId.generate(),))
    line = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    parsed = parse_discovery_event_line(line)
    assert isinstance(parsed, ProviderDiscoverySnapshotEvent)
    assert parsed.provider_name == ProviderInstanceName("docker")
    assert len(parsed.agents) == 1
    assert len(parsed.hosts) == 1
    assert len(parsed.unknown_agent_ids) == 1
    assert parsed.discovery_started_at == event.discovery_started_at
