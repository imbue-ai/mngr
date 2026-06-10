"""Unit tests for the bidirectional relay helper."""

import socket
import threading

from imbue.mngr_forward.relay import relay_data


class FakeEofChannel:
    """Stub paramiko-channel-like that simulates the half-closed (EOF-received) state.

    paramiko marks ``Channel.fileno()`` as readable for any channel event
    (data, EOF, window-adjust). The real paramiko channel after the remote
    sends EOF reports ``recv_ready() is False`` and ``eof_received is True``,
    yet ``select`` still wakes up on the fileno. This stub reproduces that
    state by wrapping a Unix socket whose peer has been closed: ``select``
    will mark it readable indefinitely. The relay must detect the EOF and
    terminate, otherwise the loop spins at ~1M iters/sec.
    """

    _sock: socket.socket

    @classmethod
    def create(cls) -> "FakeEofChannel":
        sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        sock_b.close()
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_sock", sock_a)
        return instance

    def fileno(self) -> int:
        return self._sock.fileno()

    def recv_ready(self) -> bool:
        return False

    @property
    def eof_received(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return False

    def recv(self, size: int) -> bytes:
        return b""

    def sendall(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        self._sock.close()


class _SocketBackedChannel:
    """Stub paramiko-channel-like that wraps a real socket.

    Used by the round-trip relay test below to drive ``relay_data`` against a
    pair of real sockets without spinning up a paramiko transport.
    """

    _sock: socket.socket

    @classmethod
    def create(cls, sock: socket.socket) -> "_SocketBackedChannel":
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


def test_relay_data_forwards_between_socket_pair() -> None:
    """Data sent on one end of a socketpair reaches the other via the relay."""
    app_sock, relay_sock_a = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    channel_sock, relay_sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    fake_channel = _SocketBackedChannel.create(relay_sock_b)
    relay_thread = threading.Thread(target=relay_data, args=(relay_sock_a, fake_channel), daemon=True)
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


def test_relay_data_terminates_when_channel_has_received_eof() -> None:
    """Regression: half-closed channel (EOF received) must not spin the relay loop.

    Before the fix, ``relay_step`` fell through when ``select`` reported the
    channel readable but ``recv_ready()`` was False, causing the relay thread
    to spin at hundreds of thousands of iters/sec and pin a CPU core. The
    fix detects ``eof_received``/``closed`` in that branch and terminates.
    """
    sock_a, sock_b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    eof_channel = FakeEofChannel.create()
    relay_thread = threading.Thread(target=relay_data, args=(sock_a, eof_channel), daemon=True)
    relay_thread.start()
    relay_thread.join(timeout=3.0)
    try:
        assert not relay_thread.is_alive(), (
            "relay thread should have terminated on EOF-received channel; without the fix it spins forever"
        )
    finally:
        sock_b.close()
