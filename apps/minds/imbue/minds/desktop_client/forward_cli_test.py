"""Unit tests for the minds-side wrapper around the ``mngr forward`` plugin.

Subprocess spawning (real ``mngr forward`` children) is exercised by the
acceptance / e2e tests, not here. This file constructs the
``EnvelopeStreamConsumer`` directly, attaches a fake process duck-typing
``subprocess.Popen``, and feeds canned envelope JSONL strings to its
internal envelope-line dispatcher to assert dispatching, callback firing,
and lifecycle gating.
"""

import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.forward_cli import EnvelopeStreamConsumer
from imbue.minds.desktop_client.forward_cli import LocalAgentDiscoveryHandler
from imbue.minds.desktop_client.forward_cli import MindsApiUrlWriter
from imbue.minds.desktop_client.forward_cli import ReverseTunnelEstablishedInfo
from imbue.minds.desktop_client.forward_cli import _redact_secrets
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.primitives import ServiceName
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import DiscoveryErrorEvent
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo

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


# --- observe stream: discovery error --------------------------------------


def test_discovery_error_with_provider_name_fires_provider_error_callback(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    fired: list[tuple[str, str, str]] = []
    consumer.add_on_provider_error_callback(lambda name, etype, emsg: fired.append((name, etype, emsg)))

    error_event = DiscoveryErrorEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        error_type="ImbueCloudAuthError",
        error_message="Refresh rejected by connector: token theft detected",
        source_name="discovery_poll",
        provider_name="imbue_cloud_thejash-gmail-com",
    )
    _dispatch(consumer, _observe_envelope(error_event))

    assert fired == [
        (
            "imbue_cloud_thejash-gmail-com",
            "ImbueCloudAuthError",
            "Refresh rejected by connector: token theft detected",
        )
    ]


def test_discovery_error_without_provider_name_does_not_fire_callback(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    fired: list[tuple[str, str, str]] = []
    consumer.add_on_provider_error_callback(lambda name, etype, emsg: fired.append((name, etype, emsg)))

    error_event = DiscoveryErrorEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        error_type="VpsApiError",
        error_message="VPS API error 502",
        source_name="discovery_poll",
        provider_name=None,
    )
    _dispatch(consumer, _observe_envelope(error_event))

    assert fired == []


def test_provider_error_callback_failure_does_not_block_other_callbacks(
    consumer: EnvelopeStreamConsumer,
) -> None:
    counter = [0]
    fired: list[str] = []

    def _bad_callback(_name: str, _etype: str, _emsg: str) -> None:
        raise RuntimeError("boom")

    consumer.add_on_provider_error_callback(_bad_callback)
    consumer.add_on_provider_error_callback(lambda name, _etype, _emsg: fired.append(name))

    error_event = DiscoveryErrorEvent(
        timestamp=_TIMESTAMP,
        event_id=_next_event_id(counter),
        source=_EVENT_SOURCE,
        error_type="ImbueCloudAuthError",
        error_message="msg",
        source_name="discovery_poll",
        provider_name="imbue_cloud_alice",
    )
    _dispatch(consumer, _observe_envelope(error_event))

    assert fired == ["imbue_cloud_alice"]


# --- event stream: services / requests / refresh --------------------------


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


def test_event_refresh_envelope_dispatches_to_refresh_callback(consumer: EnvelopeStreamConsumer) -> None:
    fired: list[tuple[str, str]] = []
    consumer.resolver.add_on_refresh_callback(lambda aid_str, raw: fired.append((aid_str, raw)))
    refresh_payload = {
        "timestamp": _TIMESTAMP,
        "event_id": "evt-" + "0" * 32,
        "type": "refresh",
        "source": "refresh",
        "service": "web",
    }
    _dispatch(consumer, _event_envelope(_AGENT_ID_1, refresh_payload))
    assert len(fired) == 1
    assert fired[0][0] == str(_AGENT_ID_1)


# --- forward stream: reverse_tunnel_established ---------------------------


def test_reverse_tunnel_established_fires_callback_with_parsed_info(
    consumer: EnvelopeStreamConsumer,
) -> None:
    fired: list[ReverseTunnelEstablishedInfo] = []
    consumer.add_on_reverse_tunnel_established_callback(lambda info: fired.append(info))
    payload = {
        "type": "reverse_tunnel_established",
        "agent_id": str(_AGENT_ID_1),
        "remote_port": 40000,
        "local_port": 8420,
        "ssh_host": "1.2.3.4",
        "ssh_port": 22,
    }
    _dispatch(consumer, _forward_envelope(payload, agent_id=_AGENT_ID_1))

    assert len(fired) == 1
    assert fired[0].agent_id == _AGENT_ID_1
    assert fired[0].remote_port == 40000
    assert fired[0].local_port == 8420


def test_reverse_tunnel_established_re_emit_fires_callback_unconditionally(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """Re-emit with a different remote port must call the callback again. The
    plugin is the source of truth and we must overwrite on every event.
    """
    fired: list[ReverseTunnelEstablishedInfo] = []
    consumer.add_on_reverse_tunnel_established_callback(lambda info: fired.append(info))
    base_payload = {
        "type": "reverse_tunnel_established",
        "agent_id": str(_AGENT_ID_1),
        "remote_port": 40000,
        "local_port": 8420,
        "ssh_host": "1.2.3.4",
        "ssh_port": 22,
    }
    _dispatch(consumer, _forward_envelope(base_payload, agent_id=_AGENT_ID_1))
    second_payload = dict(base_payload)
    second_payload["remote_port"] = 40001
    _dispatch(consumer, _forward_envelope(second_payload, agent_id=_AGENT_ID_1))

    assert [info.remote_port for info in fired] == [40000, 40001]


def test_reverse_tunnel_established_with_invalid_payload_is_skipped(
    consumer: EnvelopeStreamConsumer,
) -> None:
    fired: list[ReverseTunnelEstablishedInfo] = []
    consumer.add_on_reverse_tunnel_established_callback(lambda info: fired.append(info))
    # Missing remote_port -> KeyError is caught and the callback is not fired.
    bad_payload = {
        "type": "reverse_tunnel_established",
        "agent_id": str(_AGENT_ID_1),
        "local_port": 8420,
        "ssh_host": "1.2.3.4",
        "ssh_port": 22,
    }
    _dispatch(consumer, _forward_envelope(bad_payload, agent_id=_AGENT_ID_1))
    assert fired == []


# --- bounce_observe / terminate -------------------------------------------


def test_bounce_observe_sends_sighup_to_attached_pid(consumer: EnvelopeStreamConsumer) -> None:
    """Install a real SIGHUP handler in this test process and confirm the
    consumer's bounce_observe path sends SIGHUP to the configured PID.

    Uses the test's own PID so the signal really lands in the same process
    (xdist runs each worker in its own process, so this is isolated from
    parallel tests).
    """
    received = threading.Event()

    def _handler(_signo: int, _frame: object) -> None:
        received.set()

    previous = signal.signal(signal.SIGHUP, _handler)
    try:
        fake = _FakeProcess(pid=os.getpid())
        # Plugin is still running (poll() returns None).
        fake.returncode = None
        _attach_fake(consumer, fake)
        consumer.bounce_observe()
        assert received.wait(timeout=2.0), "SIGHUP was not received"
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_bounce_observe_is_no_op_when_process_already_exited(
    consumer: EnvelopeStreamConsumer,
) -> None:
    """If the plugin's poll() returns a non-None code, bounce_observe must not
    deliver a signal -- the PID could now belong to a recycled, unrelated
    process. Use a real SIGHUP handler that should never fire.
    """
    received = threading.Event()

    def _handler(_signo: int, _frame: object) -> None:
        received.set()

    previous = signal.signal(signal.SIGHUP, _handler)
    try:
        fake = _FakeProcess(pid=os.getpid())
        # Process has already exited.
        fake.returncode = 0
        _attach_fake(consumer, fake)
        consumer.bounce_observe()
        # Brief wait to confirm no signal lands.
        assert not received.wait(timeout=0.2)
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_bounce_observe_is_no_op_when_no_process_attached(consumer: EnvelopeStreamConsumer) -> None:
    # Must not raise even with no attached process.
    consumer.bounce_observe()


def test_terminate_calls_terminate_then_returns(consumer: EnvelopeStreamConsumer) -> None:
    fake = _FakeProcess(pid=4242)
    fake.returncode = 0
    _attach_fake(consumer, fake)
    consumer.terminate()
    assert fake.terminate_calls == 1


def test_terminate_is_no_op_when_no_process_attached(consumer: EnvelopeStreamConsumer) -> None:
    # Must not raise even with no attached process.
    consumer.terminate()


# --- intentional vs unintentional exit notification ---------------------------


class _RecordingNotificationDispatcher(NotificationDispatcher):
    """Test-only NotificationDispatcher that records dispatch calls instead of dispatching.

    Records into the ``recorded`` list passed at construction time. We cannot
    use a Pydantic ``PrivateAttr`` because the parent class is a ``FrozenModel``
    and we want to keep the test fixture self-contained; the list is held via
    a closure on the subclass-defined ``dispatch`` override below.
    """

    _recorded: list[tuple[NotificationRequest, str]] = PrivateAttr(default_factory=list)

    def dispatch(
        self,
        request: NotificationRequest,
        agent_display_name: str,
    ) -> None:
        self._recorded.append((request, agent_display_name))

    @property
    def recorded(self) -> list[tuple[NotificationRequest, str]]:
        return self._recorded


def _make_recording_dispatcher() -> _RecordingNotificationDispatcher:
    return _RecordingNotificationDispatcher(is_electron=False, is_macos=False)


def test_intentional_terminate_suppresses_subprocess_died_notification() -> None:
    """After consumer.terminate(), the lifecycle watcher must not surface the
    resulting non-zero exit code as a CRITICAL "Forwarding subprocess died"
    notification -- minds itself asked the subprocess to stop.
    """
    resolver = MngrCliBackendResolver()
    dispatcher = _make_recording_dispatcher()
    consumer = EnvelopeStreamConsumer(resolver=resolver, notification_dispatcher=dispatcher)
    fake = _FakeProcess(pid=4242)
    # Simulate SIGTERM -> exit code -15 after terminate() is called.
    fake.returncode = -15
    _attach_fake(consumer, fake)

    consumer.terminate()
    # Drive the lifecycle watcher synchronously; in production this runs on a
    # ConcurrencyGroup thread that calls process.wait().
    consumer._wait_and_notify_on_exit()

    assert dispatcher.recorded == [], (
        f"Intentional shutdown should not dispatch a notification, got: {dispatcher.recorded!r}"
    )


def test_unintentional_subprocess_crash_dispatches_notification() -> None:
    """If the subprocess exits non-zero without minds calling terminate(),
    the lifecycle watcher must still surface a CRITICAL notification so the
    user knows agent traffic is no longer being forwarded.
    """
    resolver = MngrCliBackendResolver()
    dispatcher = _make_recording_dispatcher()
    consumer = EnvelopeStreamConsumer(resolver=resolver, notification_dispatcher=dispatcher)
    fake = _FakeProcess(pid=4242)
    # Arbitrary non-zero crash exit code.
    fake.returncode = 17
    _attach_fake(consumer, fake)

    consumer._wait_and_notify_on_exit()

    assert len(dispatcher.recorded) == 1
    request, agent_display_name = dispatcher.recorded[0]
    assert request.title is not None
    assert "died" in request.title.lower()
    assert agent_display_name == "Minds"


def test_attach_twice_raises(consumer: EnvelopeStreamConsumer) -> None:
    fake = _FakeProcess()
    _attach_fake(consumer, fake)
    with pytest.raises(RuntimeError, match="attach already called"):
        _attach_fake(consumer, fake)


def test_start_before_attach_raises(consumer: EnvelopeStreamConsumer) -> None:
    cg = ConcurrencyGroup(name="forward-cli-test")
    with cg, pytest.raises(RuntimeError, match="start called before attach"):
        consumer.start(cg)


# --- LocalAgentDiscoveryHandler ------------------------------------------


def test_local_agent_discovery_handler_writes_minds_api_url_for_local_agent(
    tmp_path: Path,
) -> None:
    handler = LocalAgentDiscoveryHandler(
        minds_api_port=8420,
        mngr_host_dir=tmp_path / ".mngr",
    )
    handler(_AGENT_ID_1, ssh_info=None, provider_name="local")
    written = (tmp_path / ".mngr" / "agents" / str(_AGENT_ID_1) / "minds_api_url").read_text()
    assert written == "http://127.0.0.1:8420"


def test_local_agent_discovery_handler_skips_minds_api_url_for_remote_agent(
    tmp_path: Path,
) -> None:
    handler = LocalAgentDiscoveryHandler(
        minds_api_port=8420,
        mngr_host_dir=tmp_path / ".mngr",
    )
    ssh_info = RemoteSSHInfo(user="root", host="1.2.3.4", port=22, key_path=Path("/tmp/k"))
    handler(_AGENT_ID_1, ssh_info=ssh_info, provider_name="modal")
    # No minds_api_url was written under the local mngr_host_dir
    # (remote-agent writes happen via MindsApiUrlWriter via SSH).
    assert not (tmp_path / ".mngr" / "agents" / str(_AGENT_ID_1) / "minds_api_url").exists()


# --- MindsApiUrlWriter ----------------------------------------------------


def test_minds_api_url_writer_skips_when_no_ssh_info_for_agent() -> None:
    """When the resolver has no SSH info for an agent (e.g. local-only),
    MindsApiUrlWriter must short-circuit before attempting any SSH connection.
    """
    resolver = MngrCliBackendResolver()
    writer = MindsApiUrlWriter(resolver=resolver)
    # The resolver knows nothing about this agent so get_ssh_info returns None.
    info = ReverseTunnelEstablishedInfo(
        agent_id=_AGENT_ID_1,
        remote_port=40000,
        local_port=8420,
        ssh_host="1.2.3.4",
        ssh_port=22,
    )
    # Must not raise (no SSH connect attempted).
    writer(info)


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
