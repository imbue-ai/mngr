"""Unit tests for the minds-side wrapper around the ``mngr forward`` plugin.

Subprocess spawning (real ``mngr forward`` children) is exercised by the
acceptance / e2e tests, not here. This file constructs the
``EnvelopeStreamConsumer`` directly, attaches a fake process duck-typing
``subprocess.Popen``, and feeds canned envelope JSONL strings to its
internal envelope-line dispatcher to assert dispatching, callback firing,
and lifecycle gating.
"""

import json
import subprocess
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.forward_cli import _redact_secrets
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostDiscoveryEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_TIMESTAMP = IsoTimestamp("2026-05-03T00:00:00.000000000+00:00")
_EVENT_SOURCE = EventSource("mngr/discovery")
_HOST_ID_1 = HostId("host-" + "0" * 31 + "1")
_AGENT_ID_1: AgentId = AgentId("agent-" + "0" * 31 + "1")
_AGENT_ID_2: AgentId = AgentId("agent-" + "0" * 31 + "2")
_SERVICE_WEB: ServiceName = ServiceName("web")


def _next_event_id(counter: list[int]) -> EventId:
    counter[0] += 1
    return EventId(f"evt-{counter[0]:032x}")


def _make_agent(agent_id: AgentId, host_id: HostId = _HOST_ID_1) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=agent_id,
        agent_name=AgentName(f"agent-name-{agent_id[-4:]}"),
        provider_name=ProviderInstanceName("local"),
        certified_data={"labels": {}},
    )


def _serialize(event_obj: Any) -> str:
    return json.dumps(event_obj.model_dump(mode="json"))


def _observe_envelope(payload_obj: Any) -> str:
    """Wrap an event in an observe-stream envelope (matches the plugin's format)."""
    return json.dumps({"stream": "observe", "payload": json.loads(_serialize(payload_obj))})


def _event_envelope(agent_id: AgentId, payload: dict[str, Any]) -> str:
    return json.dumps({"stream": "event", "agent_id": str(agent_id), "payload": payload})


def _forward_envelope(payload: dict[str, Any], agent_id: AgentId | None = None) -> str:
    envelope: dict[str, Any] = {"stream": "forward", "payload": payload}
    if agent_id is not None:
        envelope["agent_id"] = str(agent_id)
    return json.dumps(envelope)


def _dispatch(consumer: EnvelopeStreamConsumer, line: str) -> None:
    """Test entry point that drives the consumer's internal envelope dispatcher.

    The consumer's reader threads call this same private hook for each line
    of the spawned subprocess's stdout. Tests bypass the subprocess and call
    it directly so behaviour can be asserted on canned envelope strings.
    """
    consumer._handle_envelope_line(line)


class _FakeProcess:
    """Duck-typed ``subprocess.Popen`` stand-in used for lifecycle tests.

    ``EnvelopeStreamConsumer`` only ever calls ``poll()``, ``terminate()``,
    ``kill()``, ``wait()`` and reads ``pid`` / ``stdout`` / ``stderr`` on
    its private ``_process`` attr; we expose just those.
    """

    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.stdout = None
        self.stderr = None
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_event = threading.Event()
        self.wait_event.set()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_event.wait(timeout=timeout)
        return self.returncode if self.returncode is not None else 0


def _attach_fake(consumer: EnvelopeStreamConsumer, fake: _FakeProcess) -> None:
    """Attach a duck-typed fake to the consumer.

    ``EnvelopeStreamConsumer.attach`` accepts ``subprocess.Popen[bytes]``;
    the cast here is the localised type-system escape needed because the
    fake only implements the subset of the Popen surface the consumer
    actually uses.
    """
    consumer.attach(cast(subprocess.Popen[bytes], fake))


@pytest.fixture
def consumer() -> EnvelopeStreamConsumer:
    resolver = MngrCliBackendResolver()
    return EnvelopeStreamConsumer(resolver=resolver)


# --- envelope dispatch ----------------------------------------------------


def test_invalid_json_envelope_is_skipped(consumer: EnvelopeStreamConsumer) -> None:
    # Should not raise; a warning is logged.
    _dispatch(consumer, "not json at all")
    _dispatch(consumer, "")
    _dispatch(consumer, "   \n")


def test_unknown_stream_value_is_ignored(consumer: EnvelopeStreamConsumer) -> None:
    _dispatch(consumer, json.dumps({"stream": "bogus", "payload": {"foo": 1}}))
    assert consumer.resolver.list_known_agent_ids() == ()


def test_envelope_with_non_dict_payload_is_ignored(consumer: EnvelopeStreamConsumer) -> None:
    _dispatch(consumer, json.dumps({"stream": "observe", "payload": "not-a-dict"}))
    assert consumer.resolver.list_known_agent_ids() == ()


# --- observe stream: full snapshot ----------------------------------------


def test_full_snapshot_populates_resolver_and_fires_discovered_callbacks(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    discovered: list[tuple[AgentId, RemoteSSHInfo | None, str]] = []
    consumer.add_on_agent_discovered_callback(lambda aid, ssh, prov: discovered.append((aid, ssh, prov)))

    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1), _make_agent(_AGENT_ID_2)),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    known = set(consumer.resolver.list_known_agent_ids())
    assert known == {_AGENT_ID_1, _AGENT_ID_2}
    assert {entry[0] for entry in discovered} == {_AGENT_ID_1, _AGENT_ID_2}
    # No SSH info has been emitted yet, so all agents look local from the
    # snapshot's perspective.
    assert all(entry[1] is None for entry in discovered)
    # Provider name passthrough.
    assert all(entry[2] == "local" for entry in discovered)


def test_full_snapshot_freshness_uses_producer_timestamp(consumer: EnvelopeStreamConsumer) -> None:
    """``last_full_snapshot_at`` reflects the producer's poll time, not receive time.

    The recovery redirect compares the snapshot timestamp against a locally-recorded
    outage onset, so it must be *when discovery observed the world* (the envelope
    timestamp), not when this consumer happened to read the line.
    """
    counter = [0]
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    last_event_at, last_full_snapshot_at = consumer.resolver.get_freshness_timestamps()
    expected = datetime(2026, 5, 3, 0, 0, 0, tzinfo=timezone.utc)
    assert last_full_snapshot_at == expected
    assert last_event_at == expected


def test_full_snapshot_freshness_falls_back_to_receive_time_on_bad_timestamp(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """An unparseable envelope timestamp falls back to the consumer's receive time."""
    counter = [0]
    before = datetime.now(timezone.utc)
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=IsoTimestamp("not-a-real-timestamp"),
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))
    after = datetime.now(timezone.utc)

    _, last_full_snapshot_at = consumer.resolver.get_freshness_timestamps()
    assert last_full_snapshot_at is not None
    assert before <= last_full_snapshot_at <= after


def test_subsequent_snapshot_fires_destroyed_for_dropped_agents(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    destroyed: list[AgentId] = []
    consumer.add_on_agent_destroyed_callback(lambda aid: destroyed.append(aid))

    first = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1), _make_agent(_AGENT_ID_2)),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(first))

    second = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(second))

    assert destroyed == [_AGENT_ID_2]
    assert set(consumer.resolver.list_known_agent_ids()) == {_AGENT_ID_1}


def test_snapshot_retains_agent_whose_provider_errored_then_drops_on_clean(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """An agent omitted because its provider errored is retained (and surfaced stale); a clean snapshot drops it."""
    counter = [0]
    destroyed: list[AgentId] = []
    consumer.add_on_agent_destroyed_callback(lambda aid: destroyed.append(aid))

    first = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1), _make_agent(_AGENT_ID_2)),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(first))

    # Snapshot omits agent 2 but its provider 'local' errored: agent 2 is
    # retained in the resolver (no destroyed callback) and the error is
    # surfaced so the workspace list can render it stale.
    errored = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
        error_by_provider_name={
            ProviderInstanceName("local"): DiscoveryError(
                type_name="RuntimeError",
                message="discovery failed",
                provider_name=ProviderInstanceName("local"),
            )
        },
    )
    _dispatch(consumer, _observe_envelope(errored))
    assert destroyed == []
    assert set(consumer.resolver.list_known_agent_ids()) == {_AGENT_ID_1, _AGENT_ID_2}
    assert ProviderInstanceName("local") in consumer.resolver.get_provider_errors()

    # Clean snapshot (no provider error) still omits agent 2 -> dropped now.
    clean = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(clean))
    assert destroyed == [_AGENT_ID_2]
    assert set(consumer.resolver.list_known_agent_ids()) == {_AGENT_ID_1}


# --- observe stream: host ssh info ----------------------------------------


def test_host_ssh_info_refires_discovery_with_ssh_info(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    discovered: list[tuple[AgentId, RemoteSSHInfo | None, str]] = []
    consumer.add_on_agent_discovered_callback(lambda aid, ssh, prov: discovered.append((aid, ssh, prov)))

    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1, host_id=_HOST_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    ssh_event = HostSSHInfoEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        host_id=_HOST_ID_1,
        ssh=SSHInfo(
            user="root",
            host="1.2.3.4",
            port=22,
            key_path=Path("/tmp/k"),
            command="ssh -i /tmp/k -p 22 root@1.2.3.4",
        ),
    )
    _dispatch(consumer, _observe_envelope(ssh_event))

    # First emit (from snapshot) had ssh_info=None; second emit (after
    # HOST_SSH_INFO) has the populated SSH info.
    assert len(discovered) == 2
    assert discovered[0][1] is None
    second = discovered[1][1]
    assert second is not None
    assert second.user == "root"
    assert second.host == "1.2.3.4"


# --- observe stream: agent / host destroyed -------------------------------


def test_agent_destroyed_clears_resolver_services_and_fires_callback(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    destroyed: list[AgentId] = []
    consumer.add_on_agent_destroyed_callback(lambda aid: destroyed.append(aid))

    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))
    # Seed a service so we can confirm it's cleared on destruction.
    consumer.resolver.update_services(_AGENT_ID_1, {"web": "http://127.0.0.1:9100"})

    destroyed_event = AgentDestroyedEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agent_id=_AGENT_ID_1,
        host_id=_HOST_ID_1,
    )
    _dispatch(consumer, _observe_envelope(destroyed_event))

    assert destroyed == [_AGENT_ID_1]
    assert consumer.resolver.list_known_agent_ids() == ()
    assert consumer.resolver.list_services_for_agent(_AGENT_ID_1) == ()


def test_host_destroyed_destroys_all_agents_on_host(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    destroyed: list[AgentId] = []
    consumer.add_on_agent_destroyed_callback(lambda aid: destroyed.append(aid))

    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(
            _make_agent(_AGENT_ID_1, host_id=_HOST_ID_1),
            _make_agent(_AGENT_ID_2, host_id=_HOST_ID_1),
        ),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    host_destroyed = HostDestroyedEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        host_id=_HOST_ID_1,
        agent_ids=(_AGENT_ID_1, _AGENT_ID_2),
    )
    _dispatch(consumer, _observe_envelope(host_destroyed))

    assert set(destroyed) == {_AGENT_ID_1, _AGENT_ID_2}
    assert consumer.resolver.list_known_agent_ids() == ()


# --- observe stream: host state threading ---------------------------------


def _make_host(host_id: HostId, state: HostState) -> DiscoveredHost:
    return DiscoveredHost(
        host_id=host_id,
        host_name=HostName(f"host-name-{host_id[-4:]}"),
        provider_name=ProviderInstanceName("local"),
        host_state=state,
    )


def test_full_snapshot_threads_host_state_into_resolver(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1, host_id=_HOST_ID_1),),
        hosts=(_make_host(_HOST_ID_1, HostState.RUNNING),),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    assert consumer.resolver.get_host_state(_HOST_ID_1) is HostState.RUNNING


def test_host_discovered_event_updates_host_state(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1, host_id=_HOST_ID_1),),
        hosts=(_make_host(_HOST_ID_1, HostState.RUNNING),),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    host_event = HostDiscoveryEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        host=_make_host(_HOST_ID_1, HostState.STOPPED),
    )
    _dispatch(consumer, _observe_envelope(host_event))

    assert consumer.resolver.get_host_state(_HOST_ID_1) is HostState.STOPPED


def test_host_destroyed_event_marks_host_state_destroyed(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1, host_id=_HOST_ID_1),),
        hosts=(_make_host(_HOST_ID_1, HostState.RUNNING),),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    host_destroyed = HostDestroyedEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        host_id=_HOST_ID_1,
        agent_ids=(_AGENT_ID_1,),
    )
    _dispatch(consumer, _observe_envelope(host_destroyed))

    assert consumer.resolver.get_host_state(_HOST_ID_1) is HostState.DESTROYED


# --- event stream: services / requests ------------------------------------


def test_event_services_envelope_updates_resolver_services(consumer: EnvelopeStreamConsumer) -> None:
    counter = [0]
    snapshot = FullDiscoverySnapshotEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        agents=(_make_agent(_AGENT_ID_1),),
        hosts=(),
    )
    _dispatch(consumer, _observe_envelope(snapshot))

    register_payload = {
        "timestamp": _TIMESTAMP,
        "event_id": "evt-" + "0" * 32,
        "type": "service_registered",
        "source": "services",
        "service": "web",
        "url": "http://127.0.0.1:9100",
    }
    _dispatch(consumer, _event_envelope(_AGENT_ID_1, register_payload))
    assert consumer.resolver.get_backend_url(_AGENT_ID_1, _SERVICE_WEB) == "http://127.0.0.1:9100"

    deregister_payload = {
        "timestamp": _TIMESTAMP,
        "event_id": "evt-" + "0" * 31 + "1",
        "type": "service_deregistered",
        "source": "services",
        "service": "web",
    }
    _dispatch(consumer, _event_envelope(_AGENT_ID_1, deregister_payload))
    assert consumer.resolver.get_backend_url(_AGENT_ID_1, _SERVICE_WEB) is None


def test_event_requests_envelope_dispatches_to_request_callback(consumer: EnvelopeStreamConsumer) -> None:
    fired: list[tuple[str, str]] = []
    consumer.resolver.add_on_request_callback(lambda aid_str, raw: fired.append((aid_str, raw)))
    request_payload = {
        "timestamp": _TIMESTAMP,
        "event_id": "evt-" + "0" * 32,
        "type": "request",
        "source": "requests",
        "request_id": "req-1",
    }
    _dispatch(consumer, _event_envelope(_AGENT_ID_1, request_payload))
    assert len(fired) == 1
    assert fired[0][0] == str(_AGENT_ID_1)


# --- forward stream: reverse_tunnel_established ---------------------------


def test_reverse_tunnel_established_is_silently_ignored(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """Minds no longer asks the plugin for per-agent reverse tunnels.

    The plugin may still emit ``reverse_tunnel_established`` envelopes
    on behalf of other callers (e.g. the latchkey supervisor); the
    consumer must drop them on the floor without crashing or routing
    them to any callback. This test pins that behaviour so a future
    consumer that re-adds a callback channel does so explicitly
    rather than by accident.
    """
    payload = {
        "type": "reverse_tunnel_established",
        "agent_id": str(_AGENT_ID_1),
        "remote_port": 40000,
        "local_port": 8420,
        "ssh_host": "1.2.3.4",
        "ssh_port": 22,
    }
    # Must not raise -- the consumer should just trace-log and move on.
    _dispatch(consumer, _forward_envelope(payload, agent_id=_AGENT_ID_1))


# --- forward stream: resolver_snapshot ------------------------------------


def test_resolver_snapshot_envelope_updates_accessor(consumer: EnvelopeStreamConsumer) -> None:
    """``resolver_snapshot`` envelopes feed the consumer's per-agent service mirror."""
    payload = {
        "type": "resolver_snapshot",
        "services_by_agent": {
            str(_AGENT_ID_1): {"system_interface": "http://127.0.0.1:9100"},
            str(_AGENT_ID_2): {"webdav": "http://127.0.0.1:9200"},
        },
    }
    _dispatch(consumer, _forward_envelope(payload))
    assert consumer.get_resolver_snapshot_for_agent(_AGENT_ID_1) == {
        "system_interface": "http://127.0.0.1:9100",
    }
    assert consumer.get_resolver_snapshot_for_agent(_AGENT_ID_2) == {
        "webdav": "http://127.0.0.1:9200",
    }


def test_resolver_snapshot_returns_empty_dict_for_unknown_agent(consumer: EnvelopeStreamConsumer) -> None:
    """Without any envelope yet, the accessor returns an empty dict (treated as ``no entry yet``)."""
    assert consumer.get_resolver_snapshot_for_agent(_AGENT_ID_1) == {}


def test_malformed_resolver_snapshot_envelope_is_dropped(consumer: EnvelopeStreamConsumer) -> None:
    """A malformed ``resolver_snapshot`` payload doesn't crash dispatch and leaves the mirror empty."""
    _dispatch(consumer, _forward_envelope({"type": "resolver_snapshot", "services_by_agent": "not-a-dict"}))
    assert consumer.get_resolver_snapshot_for_agent(_AGENT_ID_1) == {}


# --- forward stream: listening --------------------------------------------


def test_listening_envelope_unblocks_wait_for_listening_with_port(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """A `listening` forward envelope hands wait_for_listening the bound port."""
    _dispatch(consumer, _forward_envelope({"type": "listening", "host": "127.0.0.1", "port": 9137}))
    assert consumer.wait_for_listening(timeout=1.0) == 9137


def test_wait_for_listening_times_out_when_no_envelope_arrives(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """Without a `listening` envelope (e.g. the plugin died), wait returns None."""
    assert consumer.wait_for_listening(timeout=0.05) is None


def test_malformed_listening_port_is_dropped_and_waiter_keeps_waiting(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """A `listening` envelope with an unparseable port must not unblock the waiter
    with a bogus value -- it is dropped and the waiter times out instead.
    """
    _dispatch(consumer, _forward_envelope({"type": "listening", "host": "127.0.0.1", "port": "nope"}))
    assert consumer.wait_for_listening(timeout=0.05) is None


# --- terminate ------------------------------------------------------------


def test_terminate_calls_terminate_then_returns(consumer: EnvelopeStreamConsumer) -> None:
    fake = _FakeProcess(pid=4242)
    fake.returncode = 0
    _attach_fake(consumer, fake)
    consumer.terminate()
    assert fake.terminate_calls == 1


def test_terminate_is_no_op_when_no_process_attached(consumer: EnvelopeStreamConsumer) -> None:
    # Must not raise even with no attached process.
    consumer.terminate()


# --- intentional vs unintentional exit reporting ------------------------------


def test_intentional_terminate_does_not_report_exit() -> None:
    """After consumer.terminate(), the lifecycle watcher must not report the
    resulting exit to the on_unexpected_exit callbacks -- minds itself asked
    the subprocess to stop, so the pipeline is not unexpectedly down.
    """
    resolver = MngrCliBackendResolver()
    consumer = EnvelopeStreamConsumer(resolver=resolver)
    reported: list[int] = []
    consumer.add_on_unexpected_exit_callback(reported.append)
    fake = _FakeProcess(pid=4242)
    # Simulate SIGTERM -> exit code -15 after terminate() is called.
    fake.returncode = -15
    _attach_fake(consumer, fake)

    consumer.terminate()
    # Drive the lifecycle watcher synchronously; in production this runs on a
    # ConcurrencyGroup thread that calls process.wait().
    consumer._wait_and_report_exit()

    assert reported == [], f"Intentional shutdown should not report an exit, got: {reported!r}"


def test_unintentional_subprocess_exit_reports_to_callback() -> None:
    """If the subprocess exits without minds calling terminate(), the lifecycle
    watcher reports the exit code once to the on_unexpected_exit callbacks so
    the watchdog can transition the app-global state to BLOCKED.
    """
    resolver = MngrCliBackendResolver()
    consumer = EnvelopeStreamConsumer(resolver=resolver)
    reported: list[int] = []
    consumer.add_on_unexpected_exit_callback(reported.append)
    fake = _FakeProcess(pid=4242)
    # Arbitrary non-zero crash exit code.
    fake.returncode = 17
    _attach_fake(consumer, fake)

    consumer._wait_and_report_exit()
    # A second drain must not re-fire (reported at most once per consumer).
    consumer._wait_and_report_exit()

    assert reported == [17]


def test_attach_twice_raises(consumer: EnvelopeStreamConsumer) -> None:
    fake = _FakeProcess()
    _attach_fake(consumer, fake)
    with pytest.raises(RuntimeError, match="attach already called"):
        _attach_fake(consumer, fake)


def test_start_before_attach_raises(consumer: EnvelopeStreamConsumer) -> None:
    cg = ConcurrencyGroup(name="forward-cli-test")
    with cg, pytest.raises(RuntimeError, match="start called before attach"):
        consumer.start(cg)


# --- _redact_secrets ------------------------------------------------------


def test_redact_secrets_masks_preauth_cookie_value() -> None:
    """The argv we log when spawning the plugin must not leak --preauth-cookie."""
    command = [
        "/usr/bin/mngr",
        "forward",
        "--host",
        "127.0.0.1",
        "--port",
        "8421",
        "--service",
        "system_interface",
        "--preauth-cookie",
        "this-is-a-secret-value",
        "--format",
        "jsonl",
    ]
    redacted = _redact_secrets(command)
    assert "this-is-a-secret-value" not in " ".join(redacted)
    assert "***" in redacted
    # The flag name itself must remain so the log retains diagnostic value.
    assert "--preauth-cookie" in redacted
    # Other args must be untouched.
    assert "system_interface" in redacted
    assert "8421" in redacted


def test_redact_secrets_is_a_no_op_when_flag_missing() -> None:
    """If --preauth-cookie is absent (e.g. future caller), redact passes the command through."""
    command = ["/usr/bin/mngr", "forward", "--port", "8421"]
    assert _redact_secrets(command) == command


def test_redact_secrets_does_not_mutate_input() -> None:
    """Must return a copy -- the caller still uses the original argv to spawn Popen."""
    command = ["mngr", "forward", "--preauth-cookie", "secret"]
    original = list(command)
    _redact_secrets(command)
    assert command == original
