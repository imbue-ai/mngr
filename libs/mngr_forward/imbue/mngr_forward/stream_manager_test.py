"""Unit tests for ForwardStreamManager.

Covers the line-handling surface, driving the private ``_on_observe_output`` /
``_on_event_output`` hooks with canned JSONL, and the spawn / respawn-pacing
surface, driving ``_start_events_stream`` against a recording ConcurrencyGroup
double so no real child processes are launched.
"""

import io
import json
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from loguru import logger

from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.api.discovery_events import make_provider_discovery_snapshot_event
from imbue.mngr.errors import EXIT_CODE_TARGET_NOT_FOUND
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.stream_manager import ForwardStreamManager
from imbue.mngr_forward.stream_manager import _EVENTS_STREAM_HEALTHY_AGE_SECONDS
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2

_TIMESTAMP = IsoTimestamp("2026-05-03T00:00:00.000000000+00:00")
_EVENT_SOURCE = EventSource("mngr/discovery")
_HOST_ID = HostId("host-" + "0" * 31 + "1")
# Instance keys for the two canned agents on the shared test host (the
# resolver and the per-agent stream plumbing are instance-keyed).
_INSTANCE_1 = AgentInstanceKey.build(TEST_AGENT_ID_1, _HOST_ID)
_INSTANCE_2 = AgentInstanceKey.build(TEST_AGENT_ID_2, _HOST_ID)
_DISCOVERY_STARTED_AT = datetime(2026, 5, 3, 0, 0, 0, tzinfo=timezone.utc)
_DISCOVERY_FINISHED_AT = datetime(2026, 5, 3, 0, 0, 1, tzinfo=timezone.utc)


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
    return json.dumps(event_obj.model_dump(mode="json"))  # ty: ignore[unresolved-attribute]


@pytest.fixture
def setup() -> tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]]:
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    buf = io.StringIO()
    writer = EnvelopeWriter(output=buf)
    manager = ForwardStreamManager(resolver=resolver, envelope_writer=writer)
    counter = [0]
    return manager, resolver, buf, counter


def _provider_snapshot_line(
    agents: tuple[DiscoveredAgent, ...],
    counter: list[int],
    provider_name: str = "modal",
) -> str:
    del counter
    event = make_provider_discovery_snapshot_event(
        provider_name=ProviderInstanceName(provider_name),
        agents=agents,
        hosts=(),
        discovery_started_at=_DISCOVERY_STARTED_AT,
        discovery_finished_at=_DISCOVERY_FINISHED_AT,
    )
    return _serialize(event)


def _provider_snapshot_line_with_error(
    agents: tuple[DiscoveredAgent, ...],
    errored_provider_name: str,
    counter: list[int],
) -> str:
    del counter
    event = make_provider_discovery_snapshot_event(
        provider_name=ProviderInstanceName(errored_provider_name),
        agents=agents,
        hosts=(),
        discovery_started_at=_DISCOVERY_STARTED_AT,
        discovery_finished_at=_DISCOVERY_FINISHED_AT,
        error=DiscoveryError(
            type_name="RuntimeError",
            message="discovery failed",
            provider_name=ProviderInstanceName(errored_provider_name),
        ),
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


def _feed_observe(manager: ForwardStreamManager, line: str) -> None:
    """Feed one observe-stream line through the manager's private dispatcher (test hook)."""
    manager._on_observe_output(line + "\n", is_stdout=True)  # noqa: SLF001


def test_provider_snapshot_updates_resolver_and_fires_callback(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, counter = setup
    discovered: list[tuple[AgentId, RemoteSSHInfo | None, str]] = []
    manager.add_on_agent_discovered_callback(lambda agent_id, ssh, prov: discovered.append((agent_id, ssh, prov)))
    line = _provider_snapshot_line((_agent(TEST_AGENT_ID_1), _agent(TEST_AGENT_ID_2)), counter)
    _feed_observe(manager, line)
    # Resolver received both agents.
    assert set(resolver.list_known_agent_instances()) == {_INSTANCE_1, _INSTANCE_2}
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
    manager.add_on_agent_discovered_callback(lambda agent_id, ssh, prov: fired.append(agent_id))

    # Agent with no labels.workspace -> excluded.
    line = _agent_discovered_line(_agent(TEST_AGENT_ID_1, labels={}), counter)
    _feed_observe(manager, line)
    assert _INSTANCE_1 not in resolver.list_known_agent_instances()
    assert fired == []

    # Agent with labels.workspace=true -> included.
    line2 = _agent_discovered_line(_agent(TEST_AGENT_ID_2, labels={"workspace": "true"}), counter)
    _feed_observe(manager, line2)
    assert _INSTANCE_2 in resolver.list_known_agent_instances()
    assert fired == [TEST_AGENT_ID_2]


def test_provider_snapshot_retains_agent_whose_provider_errored_then_drops_on_clean(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A snapshot omitting an agent whose provider errored keeps it; a clean snapshot drops it."""
    manager, resolver, _buf, counter = setup
    destroyed: list[AgentId] = []
    manager.add_on_agent_destroyed_callback(lambda agent_id: destroyed.append(agent_id))

    # Both agents present (provider 'modal' succeeded).
    _feed_observe(manager, _provider_snapshot_line((_agent(TEST_AGENT_ID_1), _agent(TEST_AGENT_ID_2)), counter))
    assert set(resolver.list_known_agent_instances()) == {_INSTANCE_1, _INSTANCE_2}

    # Snapshot omits agent 2 but its provider 'modal' errored -> retained, no destruction.
    _feed_observe(manager, _provider_snapshot_line_with_error((_agent(TEST_AGENT_ID_1),), "modal", counter))
    assert set(resolver.list_known_agent_instances()) == {_INSTANCE_1, _INSTANCE_2}
    assert destroyed == []

    # Clean snapshot (no provider error) still omits agent 2 -> dropped now.
    _feed_observe(manager, _provider_snapshot_line((_agent(TEST_AGENT_ID_1),), counter))
    assert set(resolver.list_known_agent_instances()) == {_INSTANCE_1}
    assert destroyed == [TEST_AGENT_ID_2]


def test_agent_destroyed_clears_resolver_and_fires_callback(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, _buf, counter = setup
    destroyed: list[AgentId] = []
    manager.add_on_agent_destroyed_callback(lambda agent_id: destroyed.append(agent_id))

    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    _feed_observe(manager, discover_line)
    assert _INSTANCE_1 in resolver.list_known_agent_instances()

    destroyed_line = _agent_destroyed_line(TEST_AGENT_ID_1, _HOST_ID, counter)
    _feed_observe(manager, destroyed_line)
    assert _INSTANCE_1 not in resolver.list_known_agent_instances()
    assert destroyed == [TEST_AGENT_ID_1]


def test_host_ssh_info_propagates_to_resolver(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, _buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    _feed_observe(manager, discover_line)
    assert resolver.get_ssh_info(_INSTANCE_1) is None

    ssh_info_line = _host_ssh_info_line(_HOST_ID, counter)
    _feed_observe(manager, ssh_info_line)
    ssh = resolver.get_ssh_info(_INSTANCE_1)
    assert ssh is not None
    assert ssh.host == "1.2.3.4"


def test_event_services_updates_resolver(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    _feed_observe(manager, discover_line)

    services_line = json.dumps({"source": "services", "service": "system_interface", "url": "http://127.0.0.1:9100"})
    manager._on_event_output(services_line + "\n", is_stdout=True, instance_key=_INSTANCE_1)  # noqa: SLF001
    target = resolver.resolve(_INSTANCE_1)
    assert target is not None
    assert str(target.url).rstrip("/") == "http://127.0.0.1:9100"

    # Envelope passthrough: an "event" line tagged with the agent id appears
    # alongside the earlier observe envelope.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    event_envs = [e for e in envelopes if e["stream"] == "event"]
    assert len(event_envs) == 1
    assert event_envs[0]["agent_id"] == str(TEST_AGENT_ID_1)


def test_event_services_label_routes_origin_label_to_service(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A services event carrying a label makes the resolver route that label origin
    to the service; an event without a label falls back to routing under the name."""
    manager, resolver, _buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    manager._on_observe_output(discover_line + "\n", is_stdout=True)  # noqa: SLF001

    labeled = json.dumps(
        {
            "source": "services",
            "service": "terminal",
            "url": "http://127.0.0.1:7681",
            "label": "terminal-term1111",
        }
    )
    manager._on_event_output(labeled + "\n", is_stdout=True, instance_key=_INSTANCE_1)  # noqa: SLF001
    by_label = resolver.resolve_by_origin_label(_INSTANCE_1, "terminal-term1111")
    assert by_label is not None
    assert str(by_label.url).rstrip("/") == "http://127.0.0.1:7681"

    unlabeled = json.dumps({"source": "services", "service": "web", "url": "http://127.0.0.1:5000"})
    manager._on_event_output(unlabeled + "\n", is_stdout=True, instance_key=_INSTANCE_1)  # noqa: SLF001
    fallback = resolver.resolve_by_origin_label(_INSTANCE_1, "web")
    assert fallback is not None
    assert str(fallback.url).rstrip("/") == "http://127.0.0.1:5000"


def test_event_non_services_passthrough_only(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """`requests` and `refresh` source lines pass through but don't update the resolver's services."""
    manager, resolver, buf, counter = setup
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    _feed_observe(manager, discover_line)

    requests_line = json.dumps({"source": "requests", "type": "request_received"})
    manager._on_event_output(requests_line + "\n", is_stdout=True, instance_key=_INSTANCE_1)  # noqa: SLF001

    # Envelope was passed through.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    event_envs = [e for e in envelopes if e["stream"] == "event"]
    assert len(event_envs) == 1
    assert event_envs[0]["payload"]["source"] == "requests"
    # Resolver still doesn't have a services entry.
    assert resolver.resolve(_INSTANCE_1) is None


def test_invalid_observe_line_is_passthrough_but_not_fatal(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, resolver, buf, _counter = setup
    _feed_observe(manager, "not actually json")
    # Resolver state is untouched.
    assert resolver.list_known_agent_instances() == ()
    # Envelope passthrough wraps the raw line under {"raw": ...}.
    envelopes = [json.loads(s) for s in buf.getvalue().splitlines() if s]
    assert envelopes == [{"stream": "observe", "payload": {"raw": "not actually json"}}]


def test_blank_observe_line_is_dropped(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    manager, _resolver, buf, _counter = setup
    _feed_observe(manager, "")
    _feed_observe(manager, "   ")
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

    def ok(agent_id: AgentId, _ssh: RemoteSSHInfo | None, _prov: str) -> None:
        fired.append(agent_id)

    manager.add_on_agent_discovered_callback(boom)
    manager.add_on_agent_discovered_callback(ok)
    discover_line = _agent_discovered_line(_agent(TEST_AGENT_ID_1), counter)
    _feed_observe(manager, discover_line)
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
    assert manager._filtered_event_sources == ("services",)  # noqa: SLF001 - asserts internal state


def test_event_filters_unset_keeps_all_sources() -> None:
    """No event-include / event-exclude flags = every default source is kept."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    writer = EnvelopeWriter(output=io.StringIO())
    manager = ForwardStreamManager(resolver=resolver, envelope_writer=writer)
    assert manager._filtered_event_sources == ("services",)  # noqa: SLF001 - asserts internal state


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


class _FakeEventsProcess:
    """Stand-in for a per-agent events RunningProcess with a controllable liveness.

    ``poll()`` returns None while "alive" and a non-None return code once
    marked dead, mirroring the real RunningProcess contract used by
    ``_start_events_stream``.
    """

    def __init__(self) -> None:
        self._poll_value: int | None = None

    def mark_dead(self, returncode: int) -> None:
        self._poll_value = returncode

    def poll(self) -> int | None:
        return self._poll_value

    @property
    def returncode(self) -> int | None:
        return self._poll_value


class _RecordingConcurrencyGroup:
    """Minimal ConcurrencyGroup double that records every background spawn.

    Only the two methods ``_start_events_stream`` touches are implemented:
    ``is_shutting_down`` (always False so the spawn path runs) and
    ``run_process_in_background`` (records and returns a fresh live fake).
    """

    def __init__(self) -> None:
        self.spawned: list[_FakeEventsProcess] = []

    def is_shutting_down(self) -> bool:
        return False

    def run_process_in_background(self, **_kwargs: object) -> _FakeEventsProcess:
        process = _FakeEventsProcess()
        self.spawned.append(process)
        return process


def _start_events(manager: ForwardStreamManager, instance_key: AgentInstanceKey) -> None:
    """Invoke the manager's private per-agent events-stream starter (test hook)."""
    manager._start_events_stream(instance_key)  # noqa: SLF001


def _pacing_state(
    manager: ForwardStreamManager,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Expose the manager's private respawn-pacing dicts: (backoff, next_respawn_at, spawned_at) (test hook)."""
    backoff = manager._events_respawn_backoff_by_instance  # noqa: SLF001
    next_respawn_at = manager._events_next_respawn_at_by_instance  # noqa: SLF001
    spawned_at = manager._events_spawned_at_by_instance  # noqa: SLF001
    return backoff, next_respawn_at, spawned_at


def _install_recording_cg(manager: ForwardStreamManager, fake_cg: "_RecordingConcurrencyGroup") -> None:
    """Swap in a recording ConcurrencyGroup double so spawns are observable (test hook)."""
    manager._cg = fake_cg  # ty: ignore[invalid-assignment] # noqa: SLF001


def test_dead_events_stream_is_respawned_on_next_start(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A per-agent events stream that has exited must be respawned, not skipped.

    Regression for the forward wedging on "Loading workspace": when an agent's
    host restarts, the long-lived ``mngr event ... --follow`` child exits
    non-zero. The old guard skipped any agent already present in
    ``_events_processes`` -- including dead entries -- so the resolver's
    per-agent service map stayed empty forever and ``resolve`` returned None.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)

    # First start spawns a live stream; a second start leaves the live stream
    # alone (no duplicate spawn).
    _start_events(manager, _INSTANCE_1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 1

    # Once the stream exits (host restart broke --follow), the next start must
    # drop the dead entry and respawn a fresh one rather than skip the agent.
    fake_cg.spawned[0].mark_dead(1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2


def test_observe_via_file_tails_discovery_log_without_spawning_observe(tmp_path: Path) -> None:
    """With ``discovery_events_path`` set (``--observe-via-file``), the manager drives
    discovery by tailing a file written by another process and spawns no ``mngr observe``."""
    resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
    writer = EnvelopeWriter(output=io.StringIO())
    events_path = tmp_path / "events.jsonl"
    # event_sources=() so a discovered agent does not spawn a real per-agent
    # `mngr event` subprocess; this test only exercises the discovery tail path.
    manager = ForwardStreamManager(
        resolver=resolver,
        envelope_writer=writer,
        discovery_events_path=events_path,
        event_sources=(),
    )
    counter = [0]
    discovered: list[AgentId] = []
    manager.add_on_agent_discovered_callback(lambda agent_id, _ssh, _prov: discovered.append(agent_id))

    manager.start()
    try:
        # A separate "writer" creates the shared discovery log after the tail is running.
        events_path.write_text(_provider_snapshot_line((_agent(TEST_AGENT_ID_1),), counter) + "\n")
        poll_until(lambda: TEST_AGENT_ID_1 in discovered, timeout=5.0)
        # Discovery came purely from the file tail -- no observe subprocess was spawned.
        assert manager._observe_process is None  # noqa: SLF001 - asserts internal state
    finally:
        manager.stop()


def test_crash_looping_events_stream_respawn_is_backed_off(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A stream that dies immediately after a respawn is not respawned again within the backoff window.

    Regression for the per-source-penalty lockout: respawning an
    instantly-dying stream at the discovery snapshot cadence is a reconnect
    storm, and against an sshd with per-source penalties (OpenSSH >= 9.8
    defaults) that storm keeps the whole machine locked out of the host.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(1)
    # First death respawns immediately (the backoff window opens now).
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2

    # The respawn dies instantly too; snapshot-cadence retries inside the
    # window must NOT spawn again.
    fake_cg.spawned[1].mark_dead(1)
    _start_events(manager, _INSTANCE_1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2


def test_events_stream_backoff_resets_after_a_healthy_stream(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A stream that lived past the healthy age resets the crash-loop backoff.

    Its eventual death is a fresh incident (e.g. a host reboot weeks later),
    not a continuation of the earlier crash loop, so the next respawn should
    start from the initial backoff again.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    instance_str = str(_INSTANCE_1)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2

    # Simulate the respawned stream living past the healthy age, then dying,
    # with the previous backoff window already elapsed (test hooks on the
    # manager's private pacing state).
    now = time.monotonic()
    backoff_by_instance, next_respawn_at_by_instance, spawned_at_by_instance = _pacing_state(manager)
    spawned_at_by_instance[instance_str] = now - 3600.0
    next_respawn_at_by_instance[instance_str] = now - 1.0
    fake_cg.spawned[1].mark_dead(1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 3
    # The stored next-step backoff is initial*2 -- proof the healthy stream
    # reset the ladder rather than continuing to escalate from the crash loop.
    assert backoff_by_instance[instance_str] == 4.0


def test_crash_loop_backoff_is_not_reset_by_time_spent_waiting_in_the_window(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """Waiting out a backoff window must not count toward the healthy age.

    Healthiness is judged from the stream's actual lifetime, at the moment its
    death is first noticed. If it were re-judged on every snapshot, a corpse
    sitting through a 60s window would "age into" healthiness and reset the
    ladder to the initial backoff -- so a permanently dying stream would cycle
    2..60s,reset forever and the 60s cap could never hold.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    instance_str = str(_INSTANCE_1)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(1)
    # First death respawns immediately and stores the escalated next-step
    # backoff (4.0); the respawn dies instantly and its death is noticed by a
    # snapshot inside the window, which keeps the corpse.
    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[1].mark_dead(1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2

    # Simulate 61 seconds passing while the corpse waits (shift every stored
    # monotonic timestamp into the past), then let the next snapshot retry.
    backoff_by_instance, next_respawn_at_by_instance, spawned_at_by_instance = _pacing_state(manager)
    for pacing in (next_respawn_at_by_instance, spawned_at_by_instance):
        for key in pacing:
            pacing[key] -= 61.0
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 3
    # The ladder kept escalating (4 -> stored 8): the instantly-dead stream was
    # not misclassified as healthy just because its corpse sat for 61s.
    assert backoff_by_instance[instance_str] == 8.0


def _feed_events_stderr(manager: ForwardStreamManager, instance_key: AgentInstanceKey, line: str) -> None:
    """Deliver one stderr line from the per-agent events child (test hook)."""
    manager._on_event_output(line, is_stdout=False, instance_key=instance_key)  # noqa: SLF001


def test_events_stream_exit_log_carries_the_childs_last_stderr_lines(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """The exit/respawn log line must include the dead child's recent stderr.

    Regression for undiagnosable stream deaths: the child's stderr was logged at
    debug only, so a default-level log bundle showed hundreds of
    ``exited (returncode=1)`` lines with no reason. The tail is capped and is
    cleared per incident so a later child's exit reports its own stderr.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    infos: list[str] = []
    sink_id = logger.add(infos.append, level="INFO", format="{message}")
    try:
        _start_events(manager, _INSTANCE_1)
        # Seven stderr lines; only the newest five may survive in the tail.
        for idx in range(7):
            _feed_events_stderr(manager, _INSTANCE_1, f"stderr-line-{idx}")
        fake_cg.spawned[0].mark_dead(1)
        _start_events(manager, _INSTANCE_1)

        exit_logs = [message for message in infos if "exited (returncode=1)" in message]
        assert len(exit_logs) == 1
        assert "stderr-line-6" in exit_logs[0]
        assert "stderr-line-2" in exit_logs[0]
        assert "stderr-line-0" not in exit_logs[0]
        assert "stderr-line-1" not in exit_logs[0]

        # The second child dies without writing stderr: its exit line must not
        # replay the first child's output.
        fake_cg.spawned[1].mark_dead(1)
        backoff_by_instance, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
        next_respawn_at_by_instance[str(_INSTANCE_1)] = time.monotonic() - 1.0
        _start_events(manager, _INSTANCE_1)
        exit_logs_after = [message for message in infos if "exited (returncode=1)" in message]
        assert len(exit_logs_after) == 2
        assert "stderr-line-6" not in exit_logs_after[1]
        assert "(no stderr captured)" in exit_logs_after[1]
    finally:
        logger.remove(sink_id)


def test_gone_target_stream_backs_off_far_longer_without_touching_the_ladder(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A child that repeatedly reports its target gone waits the long flat backoff, not the crash ladder.

    Regression for the futile respawn storm: a stream whose agent no longer
    exists was respawned at the 60s ladder cap forever -- pure SSH/process
    churn that can never succeed. The child signals this with a distinct exit
    code, and two consecutive such exits are required, so a host record read
    mid-rewrite costs one ordinary ladder step instead of 15 minutes. The long
    backoff also leaves the ladder alone, so a later ordinary crash resumes it
    where this interlude left it.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    instance_str = str(_INSTANCE_1)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    before = time.monotonic()
    # One gone-target exit is not enough: it takes the ordinary ladder, so a
    # transiently unreadable host record is retried in seconds.
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 2
    backoff_by_instance, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
    assert next_respawn_at_by_instance[instance_str] - before < 60.0
    assert backoff_by_instance[instance_str] == 4.0

    # The second consecutive gone-target exit opens the long window, and leaves
    # the ladder untouched from here on.
    fake_cg.spawned[1].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    next_respawn_at_by_instance[instance_str] = time.monotonic() - 1.0
    before = time.monotonic()
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 3
    backoff_by_instance, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
    assert next_respawn_at_by_instance[instance_str] - before > 600.0
    # Still where strike one left it: the long backoff is flat, so it neither
    # consumed the ladder step nor doubled 15 minutes into it.
    assert backoff_by_instance[instance_str] == 4.0

    # Inside the long window, snapshot-cadence retries must not spawn.
    fake_cg.spawned[2].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    _start_events(manager, _INSTANCE_1)
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 3

    # Once the window passes (discovery may have been stale), it retries again.
    next_respawn_at_by_instance[instance_str] = time.monotonic() - 1.0
    _start_events(manager, _INSTANCE_1)
    assert len(fake_cg.spawned) == 4


def test_an_ordinary_exit_clears_the_gone_target_strikes(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """Only *consecutive* gone-target exits escalate; any other exit resets the count.

    Otherwise a workspace that occasionally races a host-record rewrite would
    accumulate strikes across unrelated crashes and eventually sit out 15
    minutes for a target that is alive.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    instance_str = str(_INSTANCE_1)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    _start_events(manager, _INSTANCE_1)

    # An ordinary crash in between clears the strike.
    _, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
    next_respawn_at_by_instance[instance_str] = time.monotonic() - 1.0
    fake_cg.spawned[1].mark_dead(1)
    _start_events(manager, _INSTANCE_1)

    # So this gone-target exit is strike one again, not two: ordinary ladder.
    _, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
    next_respawn_at_by_instance[instance_str] = time.monotonic() - 1.0
    fake_cg.spawned[2].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    before = time.monotonic()
    _start_events(manager, _INSTANCE_1)
    _, next_respawn_at_by_instance, _spawned_at = _pacing_state(manager)
    assert next_respawn_at_by_instance[instance_str] - before < 60.0


def test_a_healthy_run_clears_the_gone_target_strikes(
    setup: tuple[ForwardStreamManager, ForwardResolver, io.StringIO, list[int]],
) -> None:
    """A stream that ran healthily proves its target existed, so it must reset the strikes.

    Without this, "consecutive" spans unlimited wall-clock: one gone-target exit,
    then hours of healthy streaming, then a single unlucky second exit would put a
    live agent into the 15-minute backoff -- the very wedge this plugin exists to
    prevent, made rarer rather than removed.
    """
    manager, _resolver, _buf, _counter = setup
    fake_cg = _RecordingConcurrencyGroup()
    _install_recording_cg(manager, fake_cg)
    instance_str = str(_INSTANCE_1)

    _start_events(manager, _INSTANCE_1)
    fake_cg.spawned[0].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    _start_events(manager, _INSTANCE_1)

    # The respawn runs long enough to count as healthy before it dies, again
    # with the gone-target code.
    _backoff, next_respawn_at_by_instance, spawned_at_by_instance = _pacing_state(manager)
    spawned_at_by_instance[instance_str] = time.monotonic() - (_EVENTS_STREAM_HEALTHY_AGE_SECONDS + 1.0)
    next_respawn_at_by_instance[instance_str] = time.monotonic() - 1.0
    fake_cg.spawned[1].mark_dead(EXIT_CODE_TARGET_NOT_FOUND)
    before = time.monotonic()
    _start_events(manager, _INSTANCE_1)

    # The healthy run reset the count, so this is strike one: ordinary ladder,
    # not the long window.
    _backoff, next_respawn_at_by_instance, _spawned = _pacing_state(manager)
    assert next_respawn_at_by_instance[instance_str] - before < 60.0
