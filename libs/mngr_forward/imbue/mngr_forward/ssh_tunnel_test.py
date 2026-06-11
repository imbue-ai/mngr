"""Unit tests for the shared :class:`SSHTunnelManager`.

The actual SSH I/O paths (paramiko transport, direct-tcpip, reverse port
forward) require a live sshd and are exercised by the acceptance / release
tests. These unit tests cover the deterministic surfaces that don't need a
real network: the URL-parsing helper, the data shapes, and the bits of the
manager's repair / setup loops that can be driven against fakes.

The manager is the single SSH tunneling implementation in the monorepo:
``mngr forward --service`` uses its forward (direct-tcpip) path, and both
``mngr forward --reverse`` and ``mngr latchkey forward`` use its reverse
path. The agent_id-tagged setup and the
:meth:`remove_reverse_tunnels_for_agent` cleanup hook are there so the
latchkey supervisor can tear down all tunnels belonging to a destroyed
agent in one shot.
"""

import socket
import threading
import time
from pathlib import Path
from typing import cast

import paramiko
import pytest
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import ReverseTunnelInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import _ForwardedTunnelHandler
from imbue.mngr_forward.ssh_tunnel import _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS
from imbue.mngr_forward.ssh_tunnel import parse_url_host_port

# -- Test doubles ----------------------------------------------------------


class FakeChannelFromSocket:
    """Stub that wraps a real socket to provide a paramiko-Channel-like interface.

    Used in tests to simulate paramiko channels without requiring a real SSH connection.
    """

    _sock: socket.socket

    @classmethod
    def create(cls, sock: socket.socket) -> "FakeChannelFromSocket":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_sock", sock)
        return instance

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, size: int) -> bytes:
        return self._sock.recv(size)

    def recv_ready(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        self._sock.close()


class FakeSSHTransport:
    """Minimal stub for paramiko.Transport that reports an active state.

    Captures any handler passed to ``request_port_forward`` so tests can
    simulate an inbound forwarded connection by invoking the handler
    directly. This mirrors paramiko's real behavior where the handler is
    called (on paramiko's own dispatch thread) once per inbound channel.
    """

    _active: bool
    _port_forward_calls: list[tuple[str, int, object | None]]
    _cancel_port_forward_calls: list[tuple[str, int]]
    _assigned_remote_port: int

    @classmethod
    def create(cls, active: bool = True, assigned_remote_port: int = 54321) -> "FakeSSHTransport":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_active", active)
        object.__setattr__(instance, "_port_forward_calls", [])
        object.__setattr__(instance, "_cancel_port_forward_calls", [])
        object.__setattr__(instance, "_assigned_remote_port", assigned_remote_port)
        return instance

    def is_active(self) -> bool:
        return self._active

    def request_port_forward(self, address: str, port: int, handler: object | None = None) -> int:
        self._port_forward_calls.append((address, port, handler))
        return self._assigned_remote_port

    def cancel_port_forward(self, address: str, port: int) -> None:
        self._cancel_port_forward_calls.append((address, port))


class FakeSSHClient(paramiko.SSHClient):
    """Minimal paramiko.SSHClient subclass with a controllable transport for testing.

    Uses __new__ to bypass paramiko SSHClient initialization, injecting only
    the state needed for the methods under test.
    """

    _fake_transport: FakeSSHTransport

    @classmethod
    def create(cls, active: bool = True) -> "FakeSSHClient":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_fake_transport", FakeSSHTransport.create(active=active))
        return instance

    def get_transport(self) -> FakeSSHTransport:  # ty: ignore[invalid-method-override]
        return self._fake_transport

    def close(self) -> None:
        pass


def _sample_ssh_info(tmp_path: Path) -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="192.0.2.1",
        port=22,
        key_path=tmp_path / "key",
    )


def _make_manager_with_fake_connection(
    ssh_info: RemoteSSHInfo,
    fake_client: FakeSSHClient,
) -> SSHTunnelManager:
    """Create an SSHTunnelManager with a pre-injected fake SSH connection."""
    manager = SSHTunnelManager()
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        manager._connections[conn_key] = fake_client
    return manager


# -- parse_url_host_port ---------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1:9100", ("127.0.0.1", 9100)),
        ("http://localhost:9100", ("127.0.0.1", 9100)),  # localhost normalized to v4
        ("http://example.com:8080/path", ("example.com", 8080)),
        ("http://example.com/path", ("example.com", 80)),  # default http port
        ("https://example.com/path", ("example.com", 443)),  # default https port
    ],
)
def test_parse_url_host_port(url: str, expected: tuple[str, int]) -> None:
    assert parse_url_host_port(url) == expected


def test_parse_url_host_port_localhost_normalization() -> None:
    """SSH channels don't dual-stack so we always normalize localhost to 127.0.0.1."""
    host, port = parse_url_host_port("http://localhost")
    assert host == "127.0.0.1"
    assert port == 80


# -- RemoteSSHInfo ---------------------------------------------------------


def test_remote_ssh_info_round_trip() -> None:
    info = RemoteSSHInfo(user="root", host="1.2.3.4", port=22, key_path=Path("/tmp/k"))
    assert info.user == "root"
    assert info.host == "1.2.3.4"
    assert info.port == 22
    assert info.key_path == Path("/tmp/k")


def test_remote_ssh_info_is_frozen() -> None:
    info = RemoteSSHInfo(user="root", host="1.2.3.4", port=22, key_path=Path("/tmp/k"))
    with pytest.raises((ValidationError, TypeError)):
        info.user = "other"


# -- ReverseTunnelInfo / ReverseTunnelSpec ---------------------------------


def test_reverse_tunnel_info_defaults_and_optional_agent_id() -> None:
    ssh_info = RemoteSSHInfo(user="root", host="h", port=22, key_path=Path("/tmp/k"))
    bare = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=12345)
    assert bare.requested_remote_port == 0  # default: dynamic-assign sentinel
    assert bare.agent_id is None  # default: no agent association
    tagged = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=12345,
        agent_id="agent-abc",
    )
    assert tagged.agent_id == "agent-abc"


def test_reverse_tunnel_spec_remote_zero_means_dynamic() -> None:
    spec = ReverseTunnelSpec(remote_port=NonNegativeInt(0), local_port=PositiveInt(8420))
    assert spec.remote_port == 0
    assert spec.local_port == 8420


def test_reverse_tunnel_spec_local_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ReverseTunnelSpec(remote_port=NonNegativeInt(8420), local_port=0)  # ty: ignore[invalid-argument-type]


# -- SSH connection helpers ------------------------------------------------


# -- SSHTunnelManager structural -----------------------------------------


def test_ssh_tunnel_manager_cleanup_is_idempotent() -> None:
    """``cleanup`` on an unused manager must succeed without raising."""
    manager = SSHTunnelManager()
    manager.cleanup()
    # Calling twice is fine -- used by the lifespan-shutdown path which can
    # race with explicit cleanup() during error paths.
    manager.cleanup()


def test_cleanup_cancels_reverse_forward_even_when_connection_inactive(tmp_path: Path) -> None:
    """cleanup() must attempt to cancel the reverse forward even when the SSH
    connection reports inactive.

    A half-dead transport that paramiko has not yet noticed would otherwise be
    skipped, leaving the remote sshd's forwarded listener bound and orphaning
    the remote port across restarts (the next run's ``request_port_forward``
    is then denied).
    """
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=False)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    tunnel_info = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=5000)
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info

    manager.cleanup()

    assert fake_client._fake_transport._cancel_port_forward_calls == [("127.0.0.1", 5000)]


def test_ssh_tunnel_manager_repair_callback_registers() -> None:
    """``add_on_tunnel_repaired_callback`` accepts callbacks and stores them."""
    manager = SSHTunnelManager()
    received: list[ReverseTunnelInfo] = []
    manager.add_on_tunnel_repaired_callback(received.append)
    # Without a real broken tunnel we can't trigger the callback, but the
    # registration path itself must not raise.
    assert received == []
    manager.cleanup()


def test_ssh_tunnel_manager_health_check_starts_daemon_thread() -> None:
    """Verify start_reverse_tunnel_health_check creates a daemon thread."""
    manager = SSHTunnelManager()
    manager.start_reverse_tunnel_health_check()
    assert manager._health_check_thread is not None
    assert manager._health_check_thread.daemon is True
    # Starting again should be a no-op.
    first_thread = manager._health_check_thread
    manager.start_reverse_tunnel_health_check()
    assert manager._health_check_thread is first_thread
    manager.cleanup()


# -- _check_and_repair_tunnels --------------------------------------------
#
# These tests call ``_check_and_repair_tunnels`` directly (bypassing the
# 30-second wait in the health check loop) to exercise the repair logic.


class _FakeReverseTunnelManager(SSHTunnelManager):
    """Test double that overrides ``setup_reverse_tunnel`` so tests can
    exercise ``_check_and_repair_tunnels`` without a real SSH server.
    """

    _setup_calls: list[tuple[RemoteSSHInfo, int, int, str | None]] = PrivateAttr(default_factory=list)
    _setup_port: int = PrivateAttr(default=9999)
    _setup_raise: type[Exception] | None = PrivateAttr(default=None)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
        agent_id: str | None = None,
    ) -> int:
        self._setup_calls.append((ssh_info, local_port, remote_port, agent_id))
        if self._setup_raise is not None:
            raise self._setup_raise("simulated failure")
        return self._setup_port


def _make_fake_reverse_tunnel_manager(
    remote_port: int = 9999,
    raise_on_setup: type[Exception] | None = None,
) -> _FakeReverseTunnelManager:
    mgr = _FakeReverseTunnelManager()
    mgr._setup_port = remote_port
    mgr._setup_raise = raise_on_setup
    return mgr


def test_check_and_repair_tunnels_no_op_then_repairs_with_requested_port(tmp_path: Path) -> None:
    """Bundles three baseline-repair properties into a single scenario:

    1. With no tunnels registered, repair is a no-op.
    2. After registering a broken tunnel, repair calls ``setup_reverse_tunnel``.
    3. The setup call carries the tunnel's originally-requested remote port,
       so the agent-side URL stays stable across re-establishments.
    """
    manager = _make_fake_reverse_tunnel_manager(remote_port=1989)
    # (1) empty manager: tick is a no-op.
    manager._check_and_repair_tunnels()
    assert manager._setup_calls == []

    # (2) + (3): one broken tunnel with a fixed requested_remote_port.
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=1989,
        requested_remote_port=1989,
    )
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info

    manager._check_and_repair_tunnels()

    assert len(manager._setup_calls) == 1
    # _setup_calls tuple is (ssh_info, local_port, remote_port, agent_id).
    assert manager._setup_calls[0][1] == 8420
    assert manager._setup_calls[0][2] == 1989
    manager.cleanup()


def test_check_and_repair_tunnels_handles_setup_error(tmp_path: Path) -> None:
    """When ``setup_reverse_tunnel`` raises ``SSHTunnelError``, the error is logged and not propagated."""
    manager = _make_fake_reverse_tunnel_manager(raise_on_setup=SSHTunnelError)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=5000,
    )
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info

    manager._check_and_repair_tunnels()

    assert len(manager._setup_calls) == 1
    manager.cleanup()


def test_check_and_repair_tunnels_preserves_agent_id(tmp_path: Path) -> None:
    """A repair must re-tag the new tunnel with the same agent_id so subsequent
    ``remove_reverse_tunnels_for_agent`` calls still match it."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=1989)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=1989,
        requested_remote_port=1989,
        agent_id="agent-abc",
    )
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info

    manager._check_and_repair_tunnels()

    assert len(manager._setup_calls) == 1
    assert manager._setup_calls[0][3] == "agent-abc"
    manager.cleanup()


def test_check_and_repair_tunnels_skips_alive_tunnel(tmp_path: Path) -> None:
    """When a reverse tunnel's connection is still alive, it is skipped (not re-established)."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=9999)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=5000,
    )
    fake_client = FakeSSHClient.create(active=True)
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info
        manager._connections[conn_key] = fake_client

    manager._check_and_repair_tunnels()

    assert manager._setup_calls == []
    manager.cleanup()


def test_check_and_repair_tunnels_fires_on_repaired_callback(tmp_path: Path) -> None:
    """Successful repair fires every registered ``on_tunnel_repaired`` callback."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=22222)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=11111,
    )
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = tunnel_info

    received: list[ReverseTunnelInfo] = []
    manager.add_on_tunnel_repaired_callback(received.append)

    manager._check_and_repair_tunnels()

    # The fake setup_reverse_tunnel does not actually rewrite
    # ``_reverse_tunnels`` (real setup does), so we still see the old
    # tunnel_info -- but the callback firing path is what we're pinning.
    assert len(received) == 1
    assert received[0].local_port == 8420
    manager.cleanup()


# -- Exponential backoff --------------------------------------------------


def test_repair_failure_arms_backoff_and_skips_within_window(tmp_path: Path) -> None:
    """First failure arms the backoff state with a future ``next_attempt_at``,
    and a second tick during that window does not retry."""
    manager = _make_fake_reverse_tunnel_manager(raise_on_setup=SSHTunnelError)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_key = (conn_key, 8420)
    tunnel_info = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=5000)
    with manager._lock:
        manager._reverse_tunnels[tunnel_key] = tunnel_info

    # First tick records a failure and arms backoff.
    before = time.monotonic()
    manager._check_and_repair_tunnels()
    after = time.monotonic()
    with manager._lock:
        failure_state = manager._failure_state.get(tunnel_key)
    assert failure_state is not None
    assert failure_state.consecutive_failures == 1
    # First retry is 2**1 = 2s away (give or take loop time).
    assert failure_state.next_attempt_at >= before + 1.0
    assert failure_state.next_attempt_at <= after + 3.0
    assert len(manager._setup_calls) == 1
    # Second tick (well within the 2s backoff window) skips the retry.
    manager._check_and_repair_tunnels()
    assert len(manager._setup_calls) == 1
    manager.cleanup()


def test_repair_failure_backoff_is_capped(tmp_path: Path) -> None:
    """Once the exponential schedule reaches the cap, the counter stops growing."""
    manager = _make_fake_reverse_tunnel_manager(raise_on_setup=SSHTunnelError)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_key = (conn_key, 8420)
    tunnel_info = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=5000)
    with manager._lock:
        manager._reverse_tunnels[tunnel_key] = tunnel_info

    # Drive enough manual failures past the cap that the schedule must have
    # saturated. We bypass the backoff-window skip by directly calling the
    # bookkeeping helper instead of waiting between ticks.
    for _ in range(20):
        manager._record_repair_failure(tunnel_key, conn_key, tunnel_info, SSHTunnelError("x"))

    with manager._lock:
        failure_state = manager._failure_state.get(tunnel_key)
    assert failure_state is not None
    # 2**failures must remain at or above the cap once the counter has saturated.
    assert 2**failure_state.consecutive_failures >= _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS
    manager.cleanup()


def test_successful_setup_clears_failure_state(tmp_path: Path) -> None:
    """A real ``setup_reverse_tunnel`` (against a fake transport) clears the
    backoff bookkeeping for that tunnel_key."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    tunnel_key = (conn_key, 8420)
    tunnel_info = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=5000)
    with manager._lock:
        manager._reverse_tunnels[tunnel_key] = tunnel_info

    # Simulate a prior failure.
    manager._record_repair_failure(tunnel_key, conn_key, tunnel_info, SSHTunnelError("x"))
    with manager._lock:
        assert tunnel_key in manager._failure_state

    # A fresh ``setup_reverse_tunnel`` call for the same key must clear it.
    # The tunnel already exists and the connection is alive, so this path
    # short-circuits before clearing -- recreate without the pre-existing
    # entry to exercise the clear:
    with manager._lock:
        manager._reverse_tunnels.pop(tunnel_key, None)
    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)

    with manager._lock:
        assert tunnel_key not in manager._failure_state
    manager.cleanup()


def test_alive_sibling_clears_stale_failure_state(tmp_path: Path) -> None:
    """When the repair loop observes an alive connection on a tunnel that
    previously failed, it clears the stale backoff so the next failure
    starts a fresh schedule (rather than skipping for 5 minutes)."""
    manager = _make_fake_reverse_tunnel_manager()
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    tunnel_key = (conn_key, 8420)
    tunnel_info = ReverseTunnelInfo(ssh_info=ssh_info, local_port=8420, remote_port=5000)
    with manager._lock:
        manager._reverse_tunnels[tunnel_key] = tunnel_info
        manager._connections[conn_key] = FakeSSHClient.create(active=True)

    # Stale backoff entry from a previous failure cycle.
    manager._record_repair_failure(tunnel_key, conn_key, tunnel_info, SSHTunnelError("x"))
    with manager._lock:
        assert tunnel_key in manager._failure_state

    manager._check_and_repair_tunnels()

    with manager._lock:
        assert tunnel_key not in manager._failure_state
    manager.cleanup()


# -- remove_reverse_tunnels_for_agent -------------------------------------


def test_remove_reverse_tunnels_for_agent_drops_only_matching(tmp_path: Path) -> None:
    """Removing an agent's tunnels leaves other agents' tunnels (and the
    shared SSH connection) untouched, and a no-match lookup returns 0
    without side effects.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = ReverseTunnelInfo(
            ssh_info=ssh_info,
            local_port=8420,
            remote_port=5000,
            agent_id="agent-a",
        )
        manager._reverse_tunnels[(conn_key, 9001)] = ReverseTunnelInfo(
            ssh_info=ssh_info,
            local_port=9001,
            remote_port=6000,
            agent_id="agent-b",
        )

    # No-match lookup returns 0, leaves state alone.
    assert manager.remove_reverse_tunnels_for_agent("missing-agent") == 0
    with manager._lock:
        assert (conn_key, 8420) in manager._reverse_tunnels
        assert (conn_key, 9001) in manager._reverse_tunnels

    # Matching lookup drops only agent-a's tunnel; agent-b's still holds
    # the SSH connection so it must NOT be closed.
    assert manager.remove_reverse_tunnels_for_agent("agent-a") == 1
    with manager._lock:
        assert (conn_key, 8420) not in manager._reverse_tunnels
        assert (conn_key, 9001) in manager._reverse_tunnels
        assert conn_key in manager._connections
    manager.cleanup()


def test_remove_reverse_tunnels_for_agent_closes_orphan_connection(tmp_path: Path) -> None:
    """When the last tunnel on a host is dropped (and no forward tunnel uses
    the same host), the underlying SSH connection is closed too."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = ReverseTunnelInfo(
            ssh_info=ssh_info,
            local_port=8420,
            remote_port=5000,
            agent_id="agent-a",
        )

    removed = manager.remove_reverse_tunnels_for_agent("agent-a")

    assert removed == 1
    with manager._lock:
        assert (conn_key, 8420) not in manager._reverse_tunnels
        assert conn_key not in manager._connections
    manager.cleanup()


def test_remove_reverse_tunnels_for_agent_keeps_connection_for_forward_tunnel(tmp_path: Path) -> None:
    """If a forward tunnel still uses the same SSH host, removing all reverse
    tunnels for an agent must *not* close the SSH client out from under it."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = ReverseTunnelInfo(
            ssh_info=ssh_info,
            local_port=8420,
            remote_port=5000,
            agent_id="agent-a",
        )
        # Pretend a forward (direct-tcpip) tunnel is also using this host.
        manager._tunnel_socket_paths[f"{conn_key}->127.0.0.1:9100"] = Path("/tmp/dummy.sock")

    removed = manager.remove_reverse_tunnels_for_agent("agent-a")

    assert removed == 1
    with manager._lock:
        assert (conn_key, 8420) not in manager._reverse_tunnels
        # The forward tunnel is still using the connection -- it must survive.
        assert conn_key in manager._connections
    manager.cleanup()


# -- setup_reverse_tunnel -------------------------------------------------
#
# These tests inject a FakeSSHClient directly into _connections so that
# setup_reverse_tunnel can run without making real SSH connections.


def test_setup_reverse_tunnel_returns_assigned_port_and_records_info(tmp_path: Path) -> None:
    """``setup_reverse_tunnel`` returns the assigned remote port AND records
    the resulting ``ReverseTunnelInfo`` (including the optional ``agent_id``
    tag) in the manager's registry.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    remote_port = manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420, agent_id="agent-xyz")

    assert remote_port == 54321
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        tunnel_info = manager._reverse_tunnels.get((conn_key, 8420))
    assert tunnel_info is not None
    assert tunnel_info.remote_port == 54321
    assert tunnel_info.local_port == 8420
    assert tunnel_info.agent_id == "agent-xyz"
    manager.cleanup()


def test_setup_reverse_tunnel_reuses_existing_active_tunnel(tmp_path: Path) -> None:
    """When an active reverse tunnel already exists for (host, local_port),
    the same port is returned without re-issuing ``request_port_forward``."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    existing_tunnel = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=11111,
    )
    with manager._lock:
        manager._reverse_tunnels[(conn_key, 8420)] = existing_tunnel

    port = manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)

    assert port == 11111
    # Active tunnel was reused -- no new port_forward request.
    assert fake_client._fake_transport._port_forward_calls == []
    manager.cleanup()


def test_setup_reverse_tunnel_different_local_ports_produce_independent_tunnels(tmp_path: Path) -> None:
    """Two ``local_port``s on the same SSH host yield two distinct reverse tunnels.

    This is what lets multiple per-agent Latchkey tunnels coexist on a
    single SSH host without cross-routing.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)
    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=9001, remote_port=1989)

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        first = manager._reverse_tunnels.get((conn_key, 8420))
        second = manager._reverse_tunnels.get((conn_key, 9001))
    assert first is not None
    assert second is not None
    assert first.requested_remote_port == 0
    assert second.requested_remote_port == 1989
    manager.cleanup()


def test_setup_reverse_tunnel_registers_per_forward_handler(tmp_path: Path) -> None:
    """``setup_reverse_tunnel`` must register a paramiko handler per forward.

    Passing ``handler=None`` to ``request_port_forward`` would cause every
    inbound channel on the transport to land in one shared queue, silently
    cross-routing between concurrent forwards. We assert that a handler is
    present on every call.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)
    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=9001, remote_port=1989)

    calls = fake_client._fake_transport._port_forward_calls
    assert len(calls) == 2
    for address, _requested_port, handler in calls:
        assert address == "127.0.0.1"
        assert handler is not None, "request_port_forward must be called with a handler"
        assert callable(handler)
    manager.cleanup()


# -- _ForwardedTunnelHandler ----------------------------------------------
#
# These exercise the per-forward handler in isolation. The handler receives
# channels from paramiko and relays them to a specific local port. Two
# handlers built for different ``local_port`` values must stay independent;
# this is what prevents the "two reverse tunnels on one transport
# cross-route" class of bug.


def _start_echo_server(prefix: bytes) -> tuple[socket.socket, int, threading.Thread, threading.Event]:
    """Start a loopback TCP server that prepends ``prefix`` to every chunk it receives.

    Returns ``(listen_sock, port, accept_thread, stop_event)``. Close the
    listening socket and set ``stop_event`` to tear the server down.

    Using a distinct sentinel per server lets tests tell which server a
    relayed connection actually landed on, which is the whole point of the
    regression coverage below.
    """
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.bind(("127.0.0.1", 0))
    listen.listen(8)
    listen.settimeout(0.2)
    port = listen.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(2.0)
            try:
                data = conn.recv(4096)
                if data:
                    conn.sendall(prefix + data)
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=_serve, daemon=True, name=f"echo-{prefix!r}")
    thread.start()
    return listen, port, thread, stop


def test_forwarded_tunnel_handler_relays_to_local_port() -> None:
    """The handler connects its channel to ``127.0.0.1:local_port`` and relays data."""
    listen, port, accept_thread, stop = _start_echo_server(b"server-a:")
    try:
        shutdown_event = threading.Event()
        handler = _ForwardedTunnelHandler(local_port=port, shutdown_event=shutdown_event)

        channel_app, channel_relay = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        fake_channel = FakeChannelFromSocket.create(channel_relay)

        handler(cast(paramiko.Channel, fake_channel), ("10.0.0.1", 33333), ("127.0.0.1", port))

        channel_app.settimeout(3.0)
        channel_app.sendall(b"ping")
        response = channel_app.recv(4096)
        assert response == b"server-a:ping"

        channel_app.close()
    finally:
        stop.set()
        listen.close()
        accept_thread.join(timeout=3.0)


def test_forwarded_tunnel_handler_does_not_cross_route() -> None:
    """Regression: two handlers built for different local ports relay independently.

    This is the bug that caused the Latchkey issue: when paramiko's default
    queue-based accept path was used, a single transport's inbound channels
    were distributed to whichever accept-loop thread happened to wake first,
    regardless of which forward they belonged to. With per-forward handlers,
    each channel is routed strictly to the handler's configured ``local_port``.
    """
    listen_a, port_a, thread_a, stop_a = _start_echo_server(b"server-a:")
    listen_b, port_b, thread_b, stop_b = _start_echo_server(b"server-b:")
    try:
        shutdown = threading.Event()
        handler_a = _ForwardedTunnelHandler(local_port=port_a, shutdown_event=shutdown)
        handler_b = _ForwardedTunnelHandler(local_port=port_b, shutdown_event=shutdown)

        # Simulate 8 alternating inbound channels arriving from paramiko for
        # the two forwards. Each channel must reach the server its handler
        # was built for, regardless of arrival interleaving.
        for idx in range(8):
            is_a = idx % 2 == 0
            handler = handler_a if is_a else handler_b
            expected = b"server-a:" if is_a else b"server-b:"
            srv_port = port_a if is_a else port_b

            app_sock, relay_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            fake_channel = FakeChannelFromSocket.create(relay_sock)
            handler(cast(paramiko.Channel, fake_channel), ("10.0.0.1", 10000 + idx), ("127.0.0.1", srv_port))

            app_sock.settimeout(3.0)
            app_sock.sendall(b"hello")
            data = app_sock.recv(4096)
            assert data == expected + b"hello", f"iteration {idx}: got {data!r}, expected prefix {expected!r}"
            app_sock.close()
    finally:
        stop_a.set()
        stop_b.set()
        listen_a.close()
        listen_b.close()
        thread_a.join(timeout=3.0)
        thread_b.join(timeout=3.0)


class _ClosableChannel:
    """Minimal stand-in for a paramiko Channel that records whether ``close()`` was called.

    Used by the handler tests below to verify that inbound channels are not
    leaked when the handler exits early (shutdown in progress, or local
    connect failed).
    """

    _closed: threading.Event

    @classmethod
    def create(cls) -> "_ClosableChannel":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_closed", threading.Event())
        return instance

    def close(self) -> None:
        self._closed.set()

    def is_closed(self) -> bool:
        return self._closed.is_set()


def test_forwarded_tunnel_handler_closes_channel_when_shutdown() -> None:
    """When the shutdown event is already set, the handler closes the channel without connecting."""
    shutdown = threading.Event()
    shutdown.set()
    handler = _ForwardedTunnelHandler(local_port=1, shutdown_event=shutdown)

    channel = _ClosableChannel.create()
    handler(cast(paramiko.Channel, channel), ("10.0.0.1", 33333), ("127.0.0.1", 1))
    assert channel.is_closed()


def test_forwarded_tunnel_handler_closes_channel_on_connect_failure() -> None:
    """If connecting to the local port fails, the channel is closed instead of leaking."""
    shutdown = threading.Event()
    # Port 1 on loopback: connecting as non-root will reliably fail with
    # ConnectionRefusedError on both macOS and Linux.
    handler = _ForwardedTunnelHandler(local_port=1, shutdown_event=shutdown)

    channel = _ClosableChannel.create()
    handler(cast(paramiko.Channel, channel), ("10.0.0.1", 33333), ("127.0.0.1", 1))
    assert channel.is_closed()
