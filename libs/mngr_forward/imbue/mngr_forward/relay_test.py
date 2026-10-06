"""Unit tests for the bidirectional relay helper.

Every test drives a real ``paramiko.Channel`` from an in-process SSH session
(client and server ``paramiko.Transport`` joined by a socketpair). A paramiko
channel is not a socket: ``Channel.fileno()`` lazily creates an internal pipe
that paramiko signals on every channel event, and the relay's readiness checks
rest on how that pipe behaves under the selector. A socket-backed stub would not
exercise it.
"""

import contextlib
import os
import socket
import threading
from collections.abc import Iterator
from typing import Final

import paramiko
from paramiko.common import AUTH_SUCCESSFUL
from paramiko.common import OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
from paramiko.common import OPEN_SUCCEEDED

from imbue.mngr_forward.relay import relay_data

# Comfortably below the suite's per-test timeout, so a stuck relay fails as a
# readable assertion rather than an opaque pytest timeout.
_TIMEOUT_SECONDS: Final[float] = 5.0


class _AcceptingSSHServer(paramiko.ServerInterface):
    """Server-side policy that accepts an unauthenticated session channel; the transport never leaves the process."""

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session":
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username: str) -> str:
        return "none"

    def check_auth_none(self, username: str) -> int:
        return AUTH_SUCCESSFUL


@contextlib.contextmanager
def _real_ssh_channel_pair() -> Iterator[tuple[paramiko.Channel, paramiko.Channel]]:
    """Yield ``(client_channel, server_channel)`` from a real in-process SSH session.

    The client channel is the one to hand to ``relay_data``; the server channel
    is the far end the test sends and receives payloads on.
    """
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server_transport = paramiko.Transport(server_sock)
    client_transport = paramiko.Transport(client_sock)
    try:
        server_transport.add_server_key(paramiko.ECDSAKey.generate())
        # With an event, start_server returns at once instead of blocking on a
        # handshake that cannot progress until the client below starts.
        server_ready = threading.Event()
        server_transport.start_server(event=server_ready, server=_AcceptingSSHServer())

        client_transport.start_client(timeout=_TIMEOUT_SECONDS)
        client_transport.auth_none("relay-test")
        assert server_ready.wait(timeout=_TIMEOUT_SECONDS), "server transport never finished its handshake"

        client_channel = client_transport.open_session(timeout=_TIMEOUT_SECONDS)
        server_channel = server_transport.accept(timeout=_TIMEOUT_SECONDS)
        assert server_channel is not None, "server transport did not accept the session channel"
        yield client_channel, server_channel
    finally:
        client_transport.close()
        server_transport.close()


@contextlib.contextmanager
def _descriptors_below_occupied(min_fd: int) -> Iterator[None]:
    """Hold every free descriptor number below ``min_fd``, so descriptors opened inside land at or above it."""
    placeholder_fds: list[int] = []
    probe_fd = os.open(os.devnull, os.O_RDONLY)
    try:
        for _ in range(min_fd):
            placeholder_fd = os.dup(probe_fd)
            if placeholder_fd >= min_fd:
                os.close(placeholder_fd)
                break
            placeholder_fds.append(placeholder_fd)
        yield
    finally:
        for placeholder_fd in placeholder_fds:
            os.close(placeholder_fd)
        os.close(probe_fd)


def _assert_relay_round_trips(client_channel: paramiko.Channel, server_channel: paramiko.Channel) -> None:
    app_sock, relay_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    relay_thread = threading.Thread(target=relay_data, args=(relay_sock, client_channel), daemon=True)
    relay_thread.start()

    app_sock.settimeout(_TIMEOUT_SECONDS)
    server_channel.settimeout(_TIMEOUT_SECONDS)

    app_sock.sendall(b"request from the app side")
    assert server_channel.recv(4096) == b"request from the app side"
    server_channel.sendall(b"response from the far end")
    assert app_sock.recv(4096) == b"response from the far end"

    server_channel.close()
    relay_thread.join(timeout=_TIMEOUT_SECONDS)
    assert not relay_thread.is_alive(), "relay thread should terminate once the channel closes"
    app_sock.close()


def test_relay_data_forwards_data_over_a_real_paramiko_channel() -> None:
    with _real_ssh_channel_pair() as (client_channel, server_channel):
        _assert_relay_round_trips(client_channel, server_channel)


def test_relay_data_forwards_when_descriptors_are_numbered_above_select_limit(high_fd_floor: int) -> None:
    """A process holding more than FD_SETSIZE (1024) descriptors must still relay.

    The relay socket, the SSH transport's socket and the channel's internal pipe
    all land above the ceiling that select() rejects.
    """
    with _descriptors_below_occupied(high_fd_floor):
        with _real_ssh_channel_pair() as (client_channel, server_channel):
            assert client_channel.fileno() >= high_fd_floor, "setup failed to push the channel pipe past FD_SETSIZE"
            _assert_relay_round_trips(client_channel, server_channel)


def test_relay_data_terminates_when_channel_has_received_eof() -> None:
    """A half-closed channel must end the relay, not spin it.

    ``shutdown_write`` on the far end leaves the channel pipe readable (paramiko
    signals it on EOF as well as on data) while ``recv_ready()`` stays False.
    """
    with _real_ssh_channel_pair() as (client_channel, server_channel):
        sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        relay_thread = threading.Thread(target=relay_data, args=(sock_a, client_channel), daemon=True)
        relay_thread.start()

        server_channel.shutdown_write()
        relay_thread.join(timeout=_TIMEOUT_SECONDS)
        try:
            assert client_channel.eof_received, "far-end shutdown_write should have delivered EOF to the relay channel"
            assert not client_channel.recv_ready(), "an EOF-only channel must not report data ready"
            assert not relay_thread.is_alive(), "relay thread should have stopped on the EOF-received channel"
        finally:
            sock_b.close()
