import socket
import threading
from pathlib import Path
from typing import cast

import paramiko
import pytest
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import ReverseTunnelInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.ssh_tunnel import _ForwardedTunnelHandler
from imbue.minds.desktop_client.ssh_tunnel import _relay_data
from imbue.minds.desktop_client.ssh_tunnel import _ssh_connection_is_active
from imbue.minds.desktop_client.ssh_tunnel import _ssh_connection_transport


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
    _assigned_remote_port: int

    @classmethod
    def create(cls, active: bool = True, assigned_remote_port: int = 54321) -> "FakeSSHTransport":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_active", active)
        object.__setattr__(instance, "_port_forward_calls", [])
        object.__setattr__(instance, "_assigned_remote_port", assigned_remote_port)
        return instance

    def is_active(self) -> bool:
        return self._active

    def request_port_forward(self, address: str, port: int, handler: object | None = None) -> int:
        self._port_forward_calls.append((address, port, handler))
        return self._assigned_remote_port

    def cancel_port_forward(self, address: str, port: int) -> None:
        pass


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

    def get_transport(self) -> FakeSSHTransport:
        return self._fake_transport

    def close(self) -> None:
        pass


# -- RemoteSSHInfo tests --


def test_remote_ssh_info_constructs_with_valid_fields() -> None:
    info = RemoteSSHInfo(
        user="root",
        host="example.com",
        port=2222,
        key_path=Path("/tmp/test_key"),
    )
    assert info.user == "root"
    assert info.host == "example.com"
    assert info.port == 2222
    assert info.key_path == Path("/tmp/test_key")


def test_remote_ssh_info_is_frozen() -> None:
    info = RemoteSSHInfo(
        user="root",
        host="example.com",
        port=2222,
        key_path=Path("/tmp/test_key"),
    )
    with pytest.raises(ValidationError):
        info.user = "other"


# -- SSH connection helper tests --


def test_ssh_connection_is_active_returns_false_for_none_transport() -> None:
    """Returns False when get_transport() returns None."""
    client = paramiko.SSHClient()
    assert _ssh_connection_is_active(client) is False


def test_ssh_connection_transport_raises_when_none() -> None:
    """Raises SSHTunnelError when transport is None."""
    client = paramiko.SSHClient()
    with pytest.raises(SSHTunnelError):
        _ssh_connection_transport(client)


# -- _relay_data tests --


def test_relay_data_forwards_between_socket_pair() -> None:
    """Data sent on one end of a socketpair reaches the other via relay."""
    app_sock, relay_sock_a = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    channel_sock, relay_sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    fake_channel = FakeChannelFromSocket.create(relay_sock_b)
    relay_thread = threading.Thread(target=_relay_data, args=(relay_sock_a, fake_channel), daemon=True)
    relay_thread.start()

    app_sock.settimeout(3.0)
    channel_sock.settimeout(3.0)

    app_sock.sendall(b"hello from client")
    channel_sock.sendall(b"hello from backend")
    data = app_sock.recv(4096)
    assert data == b"hello from backend"

    app_sock.close()
    channel_sock.close()
    relay_thread.join(timeout=5.0)


# -- ReverseTunnelInfo --


def test_reverse_tunnel_info_stores_metadata() -> None:
    ssh_info = RemoteSSHInfo(
        user="root",
        host="192.168.1.1",
        port=22,
        key_path=Path("/tmp/test_key"),
    )
    info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=54321,
    )
    assert info.local_port == 8420
    assert info.remote_port == 54321
    assert info.requested_remote_port == 0


# -- SSHTunnelManager.cleanup / health-check --


def test_tunnel_manager_cleanup_without_tunnels() -> None:
    """Cleanup should work even when no tunnels have been created."""
    manager = SSHTunnelManager()
    manager.cleanup()


def test_tunnel_manager_health_check_starts_thread() -> None:
    """Verify start_reverse_tunnel_health_check creates a daemon thread."""
    manager = SSHTunnelManager()
    manager.start_reverse_tunnel_health_check()
    assert manager._health_check_thread is not None
    assert manager._health_check_thread.daemon is True
    # Starting again should be a no-op
    first_thread = manager._health_check_thread
    manager.start_reverse_tunnel_health_check()
    assert manager._health_check_thread is first_thread
    manager.cleanup()


# -- _check_and_repair_tunnels tests --
#
# These tests call _check_and_repair_tunnels directly (bypassing the
# 30-second wait in the health check loop) to exercise the repair logic.


class _FakeReverseTunnelManager(SSHTunnelManager):
    """Test double that overrides setup_reverse_tunnel so tests can exercise
    _check_and_repair_tunnels without a real SSH server.
    """

    _setup_calls: list[tuple[RemoteSSHInfo, int, int]] = PrivateAttr(default_factory=list)
    _setup_port: int = PrivateAttr(default=9999)
    _setup_raise: type[Exception] | None = PrivateAttr(default=None)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
    ) -> int:
        self._setup_calls.append((ssh_info, local_port, remote_port))
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


def _sample_ssh_info(tmp_path: Path) -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="192.0.2.1",
        port=22,
        key_path=tmp_path / "key",
    )


def test_check_and_repair_tunnels_does_nothing_when_no_tunnels() -> None:
    """When no reverse tunnels are registered, _check_and_repair_tunnels is a no-op."""
    manager = _make_fake_reverse_tunnel_manager()
    manager._check_and_repair_tunnels()
    assert manager._setup_calls == []
    manager.cleanup()


def test_check_and_repair_tunnels_calls_setup_for_broken_tunnel(tmp_path: Path) -> None:
    """_check_and_repair_tunnels calls setup_reverse_tunnel for tunnels with no active client."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=12345)
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
    assert manager._setup_calls[0][1] == 8420
    manager.cleanup()


def test_check_and_repair_tunnels_handles_setup_error(tmp_path: Path) -> None:
    """When setup_reverse_tunnel raises SSHTunnelError, the error is logged and not propagated."""
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


def test_check_and_repair_tunnels_preserves_requested_remote_port(tmp_path: Path) -> None:
    """A tunnel originally set up with a fixed remote port must be re-established
    using that same port, so the agent-side URL stays stable."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=1989)
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
    # Third positional arg is remote_port.
    assert manager._setup_calls[0][2] == 1989
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


# -- setup_reverse_tunnel tests --
#
# These tests inject a FakeSSHClient directly into _connections so that
# setup_reverse_tunnel can run without making real SSH connections.


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


def test_setup_reverse_tunnel_returns_assigned_port(tmp_path: Path) -> None:
    """setup_reverse_tunnel calls request_port_forward and returns the assigned port."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    remote_port = manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)

    assert remote_port == 54321
    manager.cleanup()


def test_setup_reverse_tunnel_stores_tunnel_info(tmp_path: Path) -> None:
    """After setup, the tunnel info is stored in _reverse_tunnels."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=8420)

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        tunnel_info = manager._reverse_tunnels.get((conn_key, 8420))

    assert tunnel_info is not None
    assert tunnel_info.remote_port == 54321
    assert tunnel_info.local_port == 8420
    manager.cleanup()


def test_setup_reverse_tunnel_reuses_existing_active_tunnel(tmp_path: Path) -> None:
    """When an active reverse tunnel already exists for (host, local_port),
    the same port is returned without re-issuing request_port_forward."""
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
    """Two local_ports on the same SSH host yield two distinct reverse tunnels.

    This is what lets multiple per-agent Latchkey tunnels coexist on a single
    SSH host without cross-routing.
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


# -- _ForwardedTunnelHandler tests --
#
# These exercise the per-forward handler in isolation. The handler receives
# channels from paramiko and relays them to a specific local port. Two handlers
# built for different local_ports must stay independent; this is what prevents
# the "two reverse tunnels on one transport cross-route" class of bug.


def _start_echo_server(prefix: bytes) -> tuple[socket.socket, int, threading.Thread, threading.Event]:
    """Start a loopback TCP server that prepends ``prefix`` to every chunk it receives.

    Returns ``(listen_sock, port, accept_thread, stop_event)``. Close the
    listening socket and set ``stop_event`` to tear the server down.

    Using a distinct sentinel per server lets tests tell which server a relayed
    connection actually landed on, which is the whole point of the regression
    coverage below.
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
    """The handler connects its channel to 127.0.0.1:local_port and relays data."""
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


# -- setup_reverse_tunnel handler registration tests --


def test_setup_reverse_tunnel_registers_per_forward_handler(tmp_path: Path) -> None:
    """setup_reverse_tunnel must register a paramiko handler per forward.

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
