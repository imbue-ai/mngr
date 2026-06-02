"""Unit tests for ``harden_tcp_socket``.

These tests exercise the sockopt-setting paths against a real local TCP socket
so we catch wrong sockopt-name / value mistakes that would silently no-op.
Platform-specific assertions are guarded with ``sys.platform`` so the test
file passes on both Linux and macOS without skipping the relevant branch.
"""

import socket
import sys

from imbue.mngr.utils.tcp_utils import harden_tcp_socket


def _make_tcp_socket() -> socket.socket:
    """Make a local TCP socket suitable for inspecting sockopts on.

    We don't need a connection -- sockopts that don't require an established
    handshake (keepalive, TCP_USER_TIMEOUT/TCP_RXT_CONNDROPTIME) can be set
    and read on an unconnected stream socket.
    """
    return socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_harden_tcp_socket_enables_so_keepalive() -> None:
    """SO_KEEPALIVE must end up enabled regardless of platform."""
    sock = _make_tcp_socket()
    try:
        harden_tcp_socket(sock)
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
    finally:
        sock.close()


def test_harden_tcp_socket_sets_keepalive_intervals_on_linux() -> None:
    if sys.platform != "linux":
        return
    sock = _make_tcp_socket()
    try:
        harden_tcp_socket(sock)
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 30
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
    finally:
        sock.close()


def test_harden_tcp_socket_sets_user_timeout_on_linux() -> None:
    """TCP_USER_TIMEOUT should match KEEPIDLE + KEEPINTVL * KEEPCNT (in ms)."""
    if sys.platform != "linux":
        return
    if not hasattr(socket, "TCP_USER_TIMEOUT"):
        return
    sock = _make_tcp_socket()
    try:
        harden_tcp_socket(sock)
        # 60 + 30 * 3 = 150 seconds = 150_000 milliseconds.
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 150_000
    finally:
        sock.close()


def test_harden_tcp_socket_sets_keepalive_intervals_on_darwin() -> None:
    if sys.platform != "darwin":
        return
    sock = _make_tcp_socket()
    try:
        harden_tcp_socket(sock)
        # Darwin's "idle before probes" sockopt is named TCP_KEEPALIVE, not
        # TCP_KEEPIDLE.
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE) == 60
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 30
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
    finally:
        sock.close()


def test_harden_tcp_socket_swallows_errors_on_closed_socket() -> None:
    """A bogus socket should not propagate setsockopt errors out of the helper."""
    sock = _make_tcp_socket()
    sock.close()
    # Must not raise.
    harden_tcp_socket(sock)
