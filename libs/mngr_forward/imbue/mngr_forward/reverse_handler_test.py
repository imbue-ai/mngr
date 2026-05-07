"""Unit tests for ReverseTunnelHandler.

Exercises the pure-Python surface: per-agent setup on discovery, multi-pair
--reverse fan-out, agent tracking, and re-emission on tunnel-repair
callbacks. The underlying SSHTunnelManager is replaced with a stub so no
real paramiko / network I/O happens.
"""

import io
import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import PrivateAttr

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.reverse_handler import ReverseTunnelHandler
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import ReverseTunnelInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2
from imbue.mngr_forward.testing import TEST_AGENT_ID_3


class _StubSSHTunnelManager(SSHTunnelManager):
    """SSHTunnelManager with setup + repair-callback paths replaced by stubs."""

    _setup_calls: list[tuple[RemoteSSHInfo, int, int]] = PrivateAttr(default_factory=list)
    _next_assigned_remote_port: int = PrivateAttr(default=33333)
    _repair_callbacks: list[Callable[[ReverseTunnelInfo], None]] = PrivateAttr(default_factory=list)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
    ) -> int:
        self._setup_calls.append((ssh_info, local_port, remote_port))
        if remote_port != 0:
            return remote_port
        # Dynamic-port path: assign a fresh integer per call so tests can
        # observe distinct values across repairs.
        assigned = self._next_assigned_remote_port
        self._next_assigned_remote_port += 1
        return assigned

    def add_on_tunnel_repaired_callback(
        self,
        callback: Callable[[ReverseTunnelInfo], None],
    ) -> None:
        self._repair_callbacks.append(callback)

    def fire_repair(self, info: ReverseTunnelInfo) -> None:
        """Test hook: invoke every registered repair callback once."""
        for cb in self._repair_callbacks:
            cb(info)

    @property
    def setup_calls(self) -> list[tuple[RemoteSSHInfo, int, int]]:
        return self._setup_calls


def _ssh_info(host: str = "1.2.3.4", port: int = 22) -> RemoteSSHInfo:
    return RemoteSSHInfo(user="root", host=host, port=port, key_path=Path("/tmp/k"))


def _read_envelopes(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line]


@pytest.fixture
def writer_buf() -> tuple[EnvelopeWriter, io.StringIO]:
    buf = io.StringIO()
    return EnvelopeWriter(output=buf), buf


def test_setup_emits_one_envelope_per_spec(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = writer_buf
    manager = _StubSSHTunnelManager()
    handler = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(
            ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=PositiveInt(8420)),
            ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(7777)),
        ),
    )
    info = _ssh_info()
    handler(TEST_AGENT_ID_1, info, "modal")
    envelopes = _read_envelopes(buf)
    assert len(envelopes) == 2
    local_ports = sorted(env["payload"]["local_port"] for env in envelopes)
    assert local_ports == [7777, 8420]
    assert {env["payload"]["agent_id"] for env in envelopes} == {str(TEST_AGENT_ID_1)}


def test_setup_skips_local_agents(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = writer_buf
    manager = _StubSSHTunnelManager()
    handler = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=PositiveInt(8420)),),
    )
    handler(TEST_AGENT_ID_1, None, "local")
    assert manager.setup_calls == []
    assert _read_envelopes(buf) == []


def test_repair_re_emits_for_every_tracked_agent(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = writer_buf
    manager = _StubSSHTunnelManager()
    handler = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),),
    )
    info = _ssh_info()
    handler(TEST_AGENT_ID_1, info, "modal")
    handler(TEST_AGENT_ID_2, info, "modal")

    # Two initial envelopes + two repair envelopes = four total. The repair
    # info carries a different remote_port to simulate sshd reassignment.
    initial_count = len(_read_envelopes(buf))
    assert initial_count == 2

    repaired_info = ReverseTunnelInfo(
        ssh_info=info,
        local_port=8420,
        remote_port=44444,
        requested_remote_port=0,
    )
    manager.fire_repair(repaired_info)

    envelopes = _read_envelopes(buf)
    assert len(envelopes) == 4
    repair_envelopes = envelopes[2:]
    repair_agent_ids = {env["payload"]["agent_id"] for env in repair_envelopes}
    assert repair_agent_ids == {str(TEST_AGENT_ID_1), str(TEST_AGENT_ID_2)}
    assert all(env["payload"]["remote_port"] == 44444 for env in repair_envelopes)


def test_repair_with_no_tracked_agents_emits_nothing(
    writer_buf: tuple[EnvelopeWriter, io.StringIO],
) -> None:
    writer, buf = writer_buf
    manager = _StubSSHTunnelManager()
    _ = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),),
    )
    # Sanity: handler is registered as a repair callback.
    assert manager._repair_callbacks  # noqa: SLF001 - test uses internal hook

    # Fire a repair for a tunnel no agent ever requested through this handler.
    info = _ssh_info()
    repaired_info = ReverseTunnelInfo(
        ssh_info=info,
        local_port=8420,
        remote_port=12345,
        requested_remote_port=0,
    )
    manager.fire_repair(repaired_info)
    assert _read_envelopes(buf) == []


def test_setup_for_snapshot_tracks_agents(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, buf = writer_buf
    manager = _StubSSHTunnelManager()
    handler = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),),
    )
    info = _ssh_info()
    handler.setup_for_snapshot(((TEST_AGENT_ID_1, info), (TEST_AGENT_ID_3, info)))
    initial = _read_envelopes(buf)
    assert len(initial) == 2

    repaired_info = ReverseTunnelInfo(
        ssh_info=info,
        local_port=8420,
        remote_port=99999,
        requested_remote_port=0,
    )
    manager.fire_repair(repaired_info)
    repair_envelopes = _read_envelopes(buf)[2:]
    agent_ids = {env["payload"]["agent_id"] for env in repair_envelopes}
    assert agent_ids == {str(TEST_AGENT_ID_1), str(TEST_AGENT_ID_3)}


def test_no_specs_means_no_callback_registration(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    writer, _buf = writer_buf
    manager = _StubSSHTunnelManager()
    # Construction should not register a repair callback when specs is empty.
    _ = ReverseTunnelHandler(tunnel_manager=manager, envelope_writer=writer, specs=())
    assert manager._repair_callbacks == []  # noqa: SLF001 - verifying internal state


def test_concurrent_setup_does_not_interleave(writer_buf: tuple[EnvelopeWriter, io.StringIO]) -> None:
    """The internal lock keeps the agents-by-tunnel map consistent under concurrency."""
    writer, _buf = writer_buf
    manager = _StubSSHTunnelManager()
    handler = ReverseTunnelHandler(
        tunnel_manager=manager,
        envelope_writer=writer,
        specs=(ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420)),),
    )
    info = _ssh_info()

    agents = [AgentId(f"agent-{'0' * 30}{i:02x}") for i in range(20)]

    def worker(start: int) -> None:
        for offset in range(5):
            handler(agents[start + offset], info, "modal")

    threads = [threading.Thread(target=worker, args=(i * 5,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    repaired_info = ReverseTunnelInfo(
        ssh_info=info,
        local_port=8420,
        remote_port=55555,
        requested_remote_port=0,
    )
    manager.fire_repair(repaired_info)
    # Every agent should be re-emitted exactly once.
    assert len(handler._lookup_agents_for_tunnel(repaired_info)) == 20
