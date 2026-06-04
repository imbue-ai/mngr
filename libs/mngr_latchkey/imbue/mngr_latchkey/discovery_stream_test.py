"""Unit tests for :mod:`imbue.mngr_latchkey.discovery_stream`.

Drives :class:`DiscoveryStreamConsumer` by feeding pre-built JSON event
lines into the package-private ``_on_observe_output`` callback that
:class:`ConcurrencyGroup` would normally call from the observe-subprocess
reader thread. This lets us cover the dispatch / SSH-late re-fire logic
without spawning a real ``mngr observe`` subprocess.

Callbacks are registered as bound methods on small recording helpers
that append every invocation to a list; the consumer's typed callable
callback interface lets us use plain methods without subclassing the
production discovery / destruction handlers.
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.api.discovery_events import AgentDestroyedEvent
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import DISCOVERY_EVENT_SOURCE
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import DiscoveryEventType
from imbue.mngr.api.discovery_events import FullDiscoverySnapshotEvent
from imbue.mngr.api.discovery_events import HostDestroyedEvent
from imbue.mngr.api.discovery_events import HostSSHInfoEvent
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SSHInfo
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_latchkey.discovery_stream import DiscoveryStreamConsumer

_PROVIDER_NAME: ProviderInstanceName = ProviderInstanceName("local")


# -- Test doubles ------------------------------------------------------------


class _DiscoveredCall(FrozenModel):
    """One captured ``__call__`` of the discovery handler."""

    agent_id: AgentId
    host_id: HostId
    ssh_info: RemoteSSHInfo | None
    provider_name: str


class _RecordingHandlers:
    """Collects every (discovery / destruction) callback invocation under a lock.

    Not a pydantic model: the consumer registers callbacks via
    ``add_on_*_callback`` rather than via a typed Field, so a plain
    class is the simplest way to bundle the two recorder lists with
    the lock that serialises access from the (presently single, but
    not contractually so) dispatch thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._discovered_calls: list[_DiscoveredCall] = []
        self._destroyed_calls: list[AgentId] = []

    def on_discovered(
        self,
        agent_id: AgentId,
        host_id: HostId,
        ssh_info: RemoteSSHInfo | None,
        provider_name: str,
    ) -> None:
        with self._lock:
            self._discovered_calls.append(
                _DiscoveredCall(agent_id=agent_id, host_id=host_id, ssh_info=ssh_info, provider_name=provider_name)
            )

    def on_destroyed(self, agent_id: AgentId) -> None:
        with self._lock:
            self._destroyed_calls.append(agent_id)

    @property
    def discovered_calls(self) -> tuple[_DiscoveredCall, ...]:
        with self._lock:
            return tuple(self._discovered_calls)

    @property
    def destroyed_calls(self) -> tuple[AgentId, ...]:
        with self._lock:
            return tuple(self._destroyed_calls)


# -- Helpers ------------------------------------------------------------------


def _make_consumer(cg: ConcurrencyGroup) -> tuple[DiscoveryStreamConsumer, _RecordingHandlers]:
    handlers = _RecordingHandlers()
    consumer = DiscoveryStreamConsumer(concurrency_group=cg)
    consumer.add_on_agent_discovered_callback(handlers.on_discovered)
    consumer.add_on_agent_destroyed_callback(handlers.on_destroyed)
    return consumer, handlers


def _make_agent(host_id: HostId, agent_name: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName(agent_name),
        provider_name=_PROVIDER_NAME,
    )


def _envelope_fields() -> tuple[IsoTimestamp, EventId]:
    """Construct an arbitrary-but-syntactically-valid envelope timestamp / id pair."""
    # ``IsoTimestamp`` only requires a well-formed ISO 8601 string; the
    # specific value does not matter for these tests so we use a fixed
    # constant. ``EventId`` is a free-form string.
    return IsoTimestamp("2024-01-01T00:00:00.000000000Z"), EventId(f"evt-{uuid4().hex}")


def _agent_discovery_line(agent: DiscoveredAgent) -> str:
    timestamp, event_id = _envelope_fields()
    return AgentDiscoveryEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent=agent,
    ).model_dump_json()


def _agent_destroyed_line(agent_id: AgentId, host_id: HostId) -> str:
    timestamp, event_id = _envelope_fields()
    return AgentDestroyedEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agent_id=agent_id,
        host_id=host_id,
        type=DiscoveryEventType.AGENT_DESTROYED,
    ).model_dump_json()


def _host_destroyed_line(host_id: HostId, agent_ids: Sequence[AgentId]) -> str:
    timestamp, event_id = _envelope_fields()
    return HostDestroyedEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        agent_ids=tuple(agent_ids),
        type=DiscoveryEventType.HOST_DESTROYED,
    ).model_dump_json()


def _host_ssh_info_line(host_id: HostId, ssh: SSHInfo) -> str:
    timestamp, event_id = _envelope_fields()
    return HostSSHInfoEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        host_id=host_id,
        ssh=ssh,
        type=DiscoveryEventType.HOST_SSH_INFO,
    ).model_dump_json()


def _full_snapshot_line(agents: Sequence[DiscoveredAgent]) -> str:
    timestamp, event_id = _envelope_fields()
    return FullDiscoverySnapshotEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agents=tuple(agents),
        hosts=(),
        type=DiscoveryEventType.DISCOVERY_FULL,
    ).model_dump_json()


def _make_agent_with_provider(host_id: HostId, agent_name: str, provider_name: str) -> DiscoveredAgent:
    return DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName(agent_name),
        provider_name=ProviderInstanceName(provider_name),
    )


def _full_snapshot_line_with_errors(
    agents: Sequence[DiscoveredAgent],
    errored_provider_names: Sequence[str],
) -> str:
    timestamp, event_id = _envelope_fields()
    error_by_provider_name = {
        ProviderInstanceName(name): DiscoveryError(
            type_name="RuntimeError",
            message="discovery failed",
            provider_name=ProviderInstanceName(name),
        )
        for name in errored_provider_names
    }
    return FullDiscoverySnapshotEvent(
        timestamp=timestamp,
        event_id=event_id,
        source=DISCOVERY_EVENT_SOURCE,
        agents=tuple(agents),
        hosts=(),
        error_by_provider_name=error_by_provider_name,
        type=DiscoveryEventType.DISCOVERY_FULL,
    ).model_dump_json()


def _make_ssh_info(host: str, port: int, key_path: Path) -> SSHInfo:
    return SSHInfo(user="root", host=host, port=port, key_path=key_path, command="ssh")


# -- Tests --------------------------------------------------------------------


def test_agent_discovery_without_ssh_info_fires_with_none(tmp_path: Path) -> None:
    """An agent discovered before its host's SSH info should fire once with ``ssh_info=None``."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        agent = _make_agent(HostId.generate(), "a1")
        consumer._on_observe_output(_agent_discovery_line(agent), is_stdout=True)
    assert len(handlers.discovered_calls) == 1
    call = handlers.discovered_calls[0]
    assert call.agent_id == agent.agent_id
    assert call.ssh_info is None
    assert call.provider_name == str(_PROVIDER_NAME)
    assert handlers.destroyed_calls == ()


def test_host_ssh_info_after_discovery_re_fires_with_ssh_info(tmp_path: Path) -> None:
    """When SSH info arrives later, the discovery callback re-fires with it.

    This is the load-bearing pattern the :class:`LatchkeyDiscoveryHandler`
    relies on to actually set up the reverse tunnel -- the first fire
    has no SSH info, the second one does.
    """
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent = _make_agent(host_id, "a1")
        key_path = tmp_path / "id_rsa"
        key_path.write_text("fake")

        consumer._on_observe_output(_agent_discovery_line(agent), is_stdout=True)
        consumer._on_observe_output(
            _host_ssh_info_line(host_id, _make_ssh_info("h1", 2222, key_path)),
            is_stdout=True,
        )

    assert len(handlers.discovered_calls) == 2
    first, second = handlers.discovered_calls
    assert first.agent_id == agent.agent_id
    assert first.ssh_info is None
    assert second.agent_id == agent.agent_id
    assert second.ssh_info is not None
    assert second.ssh_info.host == "h1"
    assert second.ssh_info.port == 2222
    assert second.ssh_info.key_path == key_path


def test_agent_discovery_after_known_ssh_info_fires_with_ssh_info(tmp_path: Path) -> None:
    """When SSH info is already known for a host, a fresh agent discovery fires *once* with it."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        key_path = tmp_path / "id_rsa"
        key_path.write_text("fake")
        consumer._on_observe_output(
            _host_ssh_info_line(host_id, _make_ssh_info("h2", 2200, key_path)),
            is_stdout=True,
        )
        agent = _make_agent(host_id, "a2")
        consumer._on_observe_output(_agent_discovery_line(agent), is_stdout=True)

    assert len(handlers.discovered_calls) == 1
    only_call = handlers.discovered_calls[0]
    assert only_call.ssh_info is not None
    assert only_call.ssh_info.host == "h2"
    assert only_call.ssh_info.port == 2200


def test_agent_destroyed_fires_destruction_callback(tmp_path: Path) -> None:
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent = _make_agent(host_id, "a1")
        consumer._on_observe_output(_agent_discovery_line(agent), is_stdout=True)
        consumer._on_observe_output(_agent_destroyed_line(agent.agent_id, host_id), is_stdout=True)

    assert handlers.destroyed_calls == (agent.agent_id,)


def test_host_destroyed_fires_destruction_for_every_agent_on_host(tmp_path: Path) -> None:
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent_one = _make_agent(host_id, "a1")
        agent_two = _make_agent(host_id, "a2")
        consumer._on_observe_output(_agent_discovery_line(agent_one), is_stdout=True)
        consumer._on_observe_output(_agent_discovery_line(agent_two), is_stdout=True)
        consumer._on_observe_output(
            _host_destroyed_line(host_id, (agent_one.agent_id, agent_two.agent_id)),
            is_stdout=True,
        )

    assert set(handlers.destroyed_calls) == {agent_one.agent_id, agent_two.agent_id}


def test_full_snapshot_resets_known_set(tmp_path: Path) -> None:
    """A snapshot replaces the known agent set; missing agents fire as destroyed."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent_one = _make_agent(host_id, "a1")
        agent_two = _make_agent(host_id, "a2")
        # First snapshot contains both agents.
        consumer._on_observe_output(_full_snapshot_line((agent_one, agent_two)), is_stdout=True)
        assert len(handlers.discovered_calls) == 2
        # Second snapshot omits agent_two.
        consumer._on_observe_output(_full_snapshot_line((agent_one,)), is_stdout=True)

    # The first snapshot fires two discoveries; the second fires a
    # destruction for the removed agent plus another discovery for the
    # surviving one. Order across calls is not load-bearing for this
    # test -- we just assert the multiset properties.
    discovery_agent_ids = [call.agent_id for call in handlers.discovered_calls]
    assert discovery_agent_ids.count(agent_one.agent_id) == 2
    assert discovery_agent_ids.count(agent_two.agent_id) == 1
    assert handlers.destroyed_calls == (agent_two.agent_id,)


def test_snapshot_retains_agent_whose_provider_errored_then_drops_on_clean_snapshot(tmp_path: Path) -> None:
    """A snapshot omitting an agent whose provider errored must not tear down its tunnel.

    The reverse tunnel only goes away when the destruction callback fires, so
    retaining the agent through the errored poll keeps its tunnel alive. A later
    *clean* (non-errored) snapshot that still omits it does drop it.
    """
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent = _make_agent_with_provider(host_id, "a1", "imbue_cloud")
        # First snapshot establishes the agent (its provider succeeded).
        consumer._on_observe_output(_full_snapshot_line((agent,)), is_stdout=True)
        assert handlers.destroyed_calls == ()
        # Second snapshot omits the agent but reports its provider errored:
        # the agent is retained, so no destruction fires.
        consumer._on_observe_output(_full_snapshot_line_with_errors((), ("imbue_cloud",)), is_stdout=True)
        assert handlers.destroyed_calls == ()
        # Third snapshot is clean (no provider error) and still omits the
        # agent: now it is genuinely gone and the destruction fires.
        consumer._on_observe_output(_full_snapshot_line(()), is_stdout=True)

    assert handlers.destroyed_calls == (agent.agent_id,)


def test_snapshot_drops_agent_when_provider_succeeded_but_omitted_it(tmp_path: Path) -> None:
    """A successful provider that simply returns fewer agents still drops the missing one."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        host_id = HostId.generate()
        agent_one = _make_agent_with_provider(host_id, "a1", "imbue_cloud")
        agent_two = _make_agent_with_provider(host_id, "a2", "imbue_cloud")
        consumer._on_observe_output(_full_snapshot_line((agent_one, agent_two)), is_stdout=True)
        # A different provider errors, but agent_two's provider (imbue_cloud)
        # succeeded and omitted it -- so agent_two is dropped, not retained.
        consumer._on_observe_output(
            _full_snapshot_line_with_errors((agent_one,), ("some_other_provider",)), is_stdout=True
        )

    assert handlers.destroyed_calls == (agent_two.agent_id,)


def test_malformed_line_is_ignored(tmp_path: Path) -> None:
    """Unparseable JSON does not crash the consumer."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        consumer._on_observe_output("{not valid json", is_stdout=True)
        consumer._on_observe_output("", is_stdout=True)
    assert handlers.discovered_calls == ()
    assert handlers.destroyed_calls == ()


def test_bounce_observe_no_op_when_not_started(tmp_path: Path) -> None:
    """``bounce_observe`` before ``start`` is a harmless no-op (no observe process to bounce)."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, _handlers = _make_consumer(cg)
        consumer.bounce_observe()


def test_stderr_line_is_dropped(tmp_path: Path) -> None:
    """Lines arriving on stderr are logged but never dispatched."""
    del tmp_path
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        consumer, handlers = _make_consumer(cg)
        agent = _make_agent(HostId.generate(), "a1")
        consumer._on_observe_output(_agent_discovery_line(agent), is_stdout=False)
    assert handlers.discovered_calls == ()
    assert handlers.destroyed_calls == ()
