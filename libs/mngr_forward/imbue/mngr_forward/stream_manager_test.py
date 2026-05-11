"""Unit tests for ForwardStreamManager's line-handling surface.

Subprocess spawning (real ConcurrencyGroup, real `mngr observe` /
`mngr event` children) is exercised by the acceptance test, not here.
This file calls the private `_on_observe_output` / `_on_event_output`
hooks directly with canned JSONL strings to exercise:

- envelope passthrough
- discovery-event routing into the resolver
- CEL filter behaviour
- on_agent_discovered / on_agent_destroyed callback firing
- service URL routing into the resolver
- bounce_observe no-op when no observe process is running
"""

import io
import json
import threading
from pathlib import Path

import pytest

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.stream_manager import ForwardStreamManager
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2

_TIMESTAMP = IsoTimestamp("2026-05-03T00:00:00.000000000+00:00")
_EVENT_SOURCE = EventSource("mngr/discovery")
_HOST_ID = HostId("host-" + "0" * 31 + "1")


def _next_event_id(counter: list[int]) -> EventId:
    counter[0] += 1
    return EventId(f"evt-{counter[0]:032x}")


def _agent(agent_id: AgentId, host_id: HostId = _HOST_ID, labels: dict[str, str] | None = None) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(f"agent-name-{agent_id[-4:]}"),
        provider_name=ProviderInstanceName("modal"),
        certified_data={"labels": labels or {}},
    )


def _serialize(event_obj: object) -> str:
    return json.dumps(event_obj.model_dump(mode="json"))  # type: ignore[attr-defined]


@pytest.fixture
def setup() -> tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]]:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    manager = ForwardStreamManager(resolver=resolver, envelope_writer=writer)
    counter = [0]
    return manager, resolver, buf, counter


def _full_snapshot_line(agents: tuple[DiscoveredAgent, ...], counter: list[int]) -> str:
    event = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=agents,
        hosts=(),
    )
    return _serialize(event)


def _agent_discovered_line(agent: DiscoveredAgent, counter: list[int]) -> str:
    event = AgentDiscoveryEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agent=agent,
    )
    return _serialize(event)


def _agent_destroyed_line(agent_id: AgentId, host_id: HostId, counter: list[int]) -> str:
    event = AgentDestroyedEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agent_id=agent_id,
        host_id=host_id,
    )
    return _serialize(event)


def _host_ssh_info_line(host_id: HostId, counter: list[int]) -> str:
    event = HostSSHInfoEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        host_id=host_id,
        ssh=SSHInfo(
            user="root",
            host="1.2.3.4",
            port=22,
            key_path=Path("/tmp/k"),
            command="ssh -i /tmp/k -p 22 root@1.2.3.4",
        ),
    )
    return _serialize(event)


def test_full_snapshot_updates_resolver_and_fires_callback(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, counter = setup
    discovered: list[tuple[AgentId, RemoteSSHInfo | None, str]] = []
    manager.add_on_agent_discovered_callback(lambda aid, ssh, prov: discovered.append((aid, ssh, prov)))
    line = _full_snapshot_line((_agent(TEST_AGENT_ID_1), _agent(TEST_AGENT_ID_2)), counter)
    manager._on_observe_output(line + "\n", is_stdout=True)  # noqa: SLF001
    # Resolver received both agents.
    assert set(resolver.list_known_agent_ids()) == {TEST_AGENT_ID_1, TEST_AGENT_ID_2}
    # Callback fired once per agent.
    assert {entry[0] for entry in discovered} == {TEST_AGENT_ID_1, TEST_AGENT_ID_2}
    # Envelope passthrough: one observe line on the writer.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    assert len(envelopes) == 1
    assert envelopes[0]["stream"] == "observe"


def test_agent_discovery_excluded_by_filter_skips_resolver(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """An agent that does not match the include filter should not register with the resolver."""
    _manager, resolver, buf, counter = setup
    # Reconstruct manager with an exclude filter on the agent id.
    manager = ForwardStreamManager(
        resolver=resolver,
        envelope_writer=EnvelopeWriter(output=buf),
        agent_include=("has(agent.labels.workspace)",),
    )
    fired: list[AgentId] = []
    manager.add_on_agent_discovered_callback(lambda aid, ssh, prov: fired.append(aid))

    # Agent with no labels.workspace -> excluded.
    line = _agent_discovered_line(_agent(TEST_AGENT_ID_1, labels={}), counter)
    manager._on_observe_output(line + "\n", is_stdout=True)  # noqa: SLF001
    assert TEST_AGENT_ID_1 not in resolver.list_known_agent_ids()
    assert fired == []

    # Agent with labels.workspace=true -> included.
    line2 = _agent_discovered_line(_agent(TEST_AGENT_ID_2, labels={"workspace": "true"}), counter)
    manager._on_observe_output(line2 + "\n", is_stdout=True)  # noqa: SLF001
    assert TEST_AGENT_ID_2 in resolver.list_known_agent_ids()
    assert fired == [TEST_AGENT_ID_2]


def test_agent_destroyed_clears_resolver_and_fires_callback(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, _buf, counter = setup
    destroyed: list[AgentId] = []
    manager.add_on_agent_destroyed_callback(lambda aid: destroyed.append(aid))

    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001
    assert TEST_AGENT_ID_1 in resolver.list_known_agent_ids()

    destroyed_line = _agent_destroyed_line(TEST_AGENT_ID_1, _HOST_ID, counter)
    manager._on_observe_output(destroyed_line + "\n", is_stdout=True)  # noqa: SLF001
    assert TEST_AGENT_ID_1 not in resolver.list_known_agent_ids()
    assert destroyed == [TEST_AGENT_ID_1]


def test_host_ssh_info_propagates_to_resolver(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, _buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001
    assert resolver.get_ssh_info(TEST_AGENT_ID_1) is None

    ssh_info_line = _host_ssh_info_line(_HOST_ID, counter)
    manager._on_observe_output(ssh_info_line + "\n", is_stdout=True)  # noqa: SLF001
    ssh = resolver.get_ssh_info(TEST_AGENT_ID_1)
    assert ssh is not None
    assert ssh.host == "1.2.3.4"


def test_event_services_updates_resolver(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001

    services_line = json.dumps({"source": "services", "service": "system_interface", "url": "http://127.0.0.1:9100"})
    manager._on_event_output(services_line + "\n", is_stdout=True, agent_id=TEST_AGENT_ID_1)  # noqa: SLF001
    target = resolver.resolve(TEST_AGENT_ID_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9100"

    # Envelope passthrough: an "event" line tagged with the agent id appears
    # alongside the earlier observe envelope.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    event_envs = [e for e in envelopes if e["stream"] == "event"]
    assert len(event_envs) == 1
    assert event_envs[0]["agent_id"] == str(TEST_AGENT_ID_1)


def test_event_non_services_passthrough_only(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """`requests` and `refresh` source lines pass through but don't update the resolver's services."""
    manager, resolver, buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001

    requests_line = json.dumps({"source": "requests", "type": "request_received"})
    manager._on_event_output(requests_line + "\n", is_stdout=True, agent_id=TEST_AGENT_ID_1)  # noqa: SLF001

    # Envelope was passed through.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    event_envs = [e for e in envelopes if e["stream"] == "event"]
    assert len(event_envs) == 1
    assert event_envs[0]["payload"]["source"] == "requests"
    # Resolver still doesn't have a services entry.
    assert resolver.resolve(TEST_AGENT_ID_1) is None


def test_invalid_observe_line_is_passthrough_but_not_fatal(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, _counter = setup
    manager._on_observe_output("not actually json\n", is_stdout=True)  # noqa: SLF001
    # Resolver state is untouched.
    assert resolver.list_known_agent_ids() == ()
    # Envelope passthrough wraps the raw line under {"raw": ...}.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    assert envelopes == [{"stream": "observe", "payload": {"raw": "not actually json"}}]


def test_blank_observe_line_is_dropped(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, _resolver, buf, _counter = setup
    manager._on_observe_output("\n", is_stdout=True)  # noqa: SLF001
    manager._on_observe_output("   \n", is_stdout=True)  # noqa: SLF001
    assert buf.getvalue() == ""


def test_observe_stderr_is_logged_not_emitted(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, _resolver, buf, _counter = setup
    manager._on_observe_output("error: something\n", is_stdout=False)  # noqa: SLF001
    # Stderr should not be passed through as an observe envelope.
    assert buf.getvalue() == ""


def test_bounce_observe_no_op_when_not_started(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, _resolver, _buf, _counter = setup
    # Should not raise even though start() was never called.
    manager.bounce_observe()


def test_callbacks_isolated_per_failure(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A raising callback must not prevent other callbacks from firing."""
    manager, _resolver, _buf, counter = setup
    fired: list[AgentId] = []

    def boom(_aid: AgentId, _ssh: RemoteSSHInfo | None, _prov: str) -> None:
        raise RuntimeError("boom")

    def ok(aid: AgentId, _ssh: RemoteSSHInfo | None, _prov: str) -> None:
        fired.append(aid)

    manager.add_on_agent_discovered_callback(boom)
    manager.add_on_agent_discovered_callback(ok)
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001
    assert fired == [TEST_AGENT_ID_1]


def test_event_include_filters_event_sources_at_startup() -> None:
    """`--event-include 'event.source == "services"'` keeps only the services source."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    writer = EnvelopeWriter(output=io.StringIO())
    manager = ForwardStreamManager(
        resolver=resolver,
        envelope_writer=writer,
        event_include=("event.source == 'services'",),
    )
    # The compiled filter should keep only the services source.
    assert manager._filtered_event_sources == ("services",)  # noqa: SLF001 - asserts internal state


def test_event_exclude_filters_event_sources_at_startup() -> None:
    """`--event-exclude 'event.source == "requests"'` drops only the requests source."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    writer = EnvelopeWriter(output=io.StringIO())
    manager = ForwardStreamManager(
        resolver=resolver,
        envelope_writer=writer,
        event_exclude=("event.source == 'requests'",),
    )
    assert manager._filtered_event_sources == ("services", "refresh")  # noqa: SLF001 - asserts internal state


def test_event_filters_unset_keeps_all_sources() -> None:
    """No event-include / event-exclude flags = every default source is kept."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    writer = EnvelopeWriter(output=io.StringIO())
    manager = ForwardStreamManager(resolver=resolver, envelope_writer=writer)
    assert manager._filtered_event_sources == (  # noqa: SLF001 - asserts internal state
        "services",
        "requests",
        "refresh",
    )


def test_multiple_observe_lines_serialize_through_envelope(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, _resolver, buf, counter = setup
    threads: list[threading.Thread] = []
    for _ in range(8):
        threads.append(
            threading.Thread(
                target=lambda: manager._on_observe_output(  # noqa: SLF001
                    _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter) + "\n",
                    is_stdout=True,
                )
            )
        )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    # Every observe envelope written under load is well-formed JSON (the
    # envelope writer holds a lock; this asserts no interleaved bytes).
    assert len(envelopes) == 8
    assert all(env["stream"] == "observe" for env in envelopes)
