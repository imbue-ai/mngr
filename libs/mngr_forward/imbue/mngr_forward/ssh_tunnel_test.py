"""Unit tests for the shared :class:`SSHTunnelManager`.

These tests cover the surfaces that can be driven deterministically: the
URL-parsing helper, the data shapes, the repair / setup loops against fakes, and
the reverse-tunnel bookkeeping.

The direct-tcpip refusal classification at the bottom of this file runs an sshd
of its own on loopback. What it settles cannot be settled against a fake by
construction -- see the comment there.

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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from typing import cast

import paramiko
import pytest
from paramiko.common import AUTH_SUCCESSFUL
from paramiko.common import OPEN_FAILED_CONNECT_FAILED
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.primitives import NonNegativeInt
from imbue.imbue_common.primitives import PositiveInt
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_forward.primitives import ReverseTunnelSpec
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import ReverseTunnelInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager
from imbue.mngr_forward.ssh_tunnel import SSHTunnelPhase
from imbue.mngr_forward.ssh_tunnel import _CHANNEL_OPEN_TIMEOUT_SECONDS
from imbue.mngr_forward.ssh_tunnel import _ForwardedTunnelHandler
from imbue.mngr_forward.ssh_tunnel import _REVERSE_TUNNEL_BACKOFF_CAP_SECONDS
from imbue.mngr_forward.ssh_tunnel import _TransportFailureHandler
from imbue.mngr_forward.ssh_tunnel import _create_short_path_tmpdir
from imbue.mngr_forward.ssh_tunnel import _create_ssh_client
from imbue.mngr_forward.ssh_tunnel import _create_tunnel_listener
from imbue.mngr_forward.ssh_tunnel import _is_transport_unusable
from imbue.mngr_forward.ssh_tunnel import _open_and_relay
from imbue.mngr_forward.ssh_tunnel import _resolve_known_hosts_path
from imbue.mngr_forward.ssh_tunnel import _tunnel_accept_loop
from imbue.mngr_forward.ssh_tunnel import parse_url_host_port

# How long a deliberately-stuck ``open_channel`` stays stuck. Must stay well
# clear of the window a test then waits for the *second* open: at equal values
# a serialized implementation could release the first open and still deliver
# the second in time, and the regression test would stop discriminating.
_BLOCKED_OPEN_HOLD_SECONDS: Final[float] = 30.0

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

    def set_active(self, active: bool) -> None:
        """Flip the reported liveness, standing in for a peer that went away mid-session."""
        object.__setattr__(self, "_active", active)

    def open_channel(
        self,
        kind: str,
        dest_addr: tuple[str, int] | None = None,
        src_addr: tuple[str, int] | None = None,
        window_size: int | None = None,
        max_packet_size: int | None = None,
        timeout: float | None = None,
    ) -> paramiko.Channel:
        """Refuse every open, the way sshd does for a target port nothing is listening on."""
        raise paramiko.ChannelException(2, "Connect failed")


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


def test_create_ssh_client_refuses_to_connect_without_a_known_hosts_file(tmp_path: Path) -> None:
    """A missing known_hosts file must be a hard error, never trust-on-first-use.

    Tagged ``LOCAL_SETUP``, and that is the load-bearing half: the raise happens
    before a packet is sent, so it is evidence about this device and nothing at
    all about the agent's host. A consumer reads the phase to decide whether to
    blame -- and restart -- the workspace, so tagging this one ``HOST_CONNECT``
    would blame a machine that was never contacted, with nothing else in the
    suite noticing.
    """
    key_path = tmp_path / "ssh_key"
    key_path.write_text("irrelevant-key-material")
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=key_path)

    with pytest.raises(SSHTunnelError, match="known_hosts") as exc_info:
        _create_ssh_client(ssh_info)
    assert exc_info.value.phase is SSHTunnelPhase.LOCAL_SETUP


def test_create_ssh_client_refuses_to_connect_without_the_private_key(tmp_path: Path) -> None:
    """A key this device does not have must be caught before the host is asked.

    The plan's own #427 case names key material alongside known_hosts, and it is
    only device-side if it is checked here: paramiko opens the key during
    authentication, after the host has answered, where a missing file is
    indistinguishable from the host rejecting the key we offered -- so it would
    surface as CONNECT_ERROR and get a machine restarted over trust material
    that never left this laptop.
    """
    key_path = tmp_path / "ssh_key"
    (tmp_path / "known_hosts").write_text("")
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=key_path)

    with pytest.raises(SSHTunnelError, match="No SSH key") as exc_info:
        _create_ssh_client(ssh_info)
    assert exc_info.value.phase is SSHTunnelPhase.LOCAL_SETUP


def test_create_ssh_client_refuses_a_key_path_that_is_not_a_file(tmp_path: Path) -> None:
    """A producer-owned key path naming a directory is refused before connecting.

    An existence check would let a directory through to paramiko, which raises
    ``IsADirectoryError``: an ``OSError``, so it escapes the ``SSHException``
    arm of paramiko's key loop and arrives untagged, read as CONNECT_ERROR.
    That blames the workspace for a key this device never had. (A record with
    no key at all -- ``Path("")``, normalised to ``.`` -- is the
    credential-deferring case instead, covered by the tests below.)
    """
    (tmp_path / "known_hosts").write_text("")

    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=tmp_path)
    with pytest.raises(SSHTunnelError, match="No SSH key file") as exc_info:
        _create_ssh_client(ssh_info)
    assert exc_info.value.phase is SSHTunnelPhase.LOCAL_SETUP


def test_create_ssh_client_refusal_names_both_candidate_paths(tmp_path: Path) -> None:
    """When an explicit path was supplied and both candidates are missing, the error names both."""
    key_path = tmp_path / "ssh_key"
    key_path.write_text("irrelevant-key-material")
    explicit_path = tmp_path / "pins" / "known_hosts"
    ssh_info = RemoteSSHInfo(
        user="root", host="203.0.113.5", port=22, key_path=key_path, known_hosts_path=explicit_path
    )

    with pytest.raises(SSHTunnelError, match=rf"{explicit_path}.*{key_path.parent / 'known_hosts'}"):
        _create_ssh_client(ssh_info)


def test_create_ssh_client_refusal_names_one_path_when_the_candidates_coincide(tmp_path: Path) -> None:
    """A producer may store known_hosts beside the key *and* name it explicitly.

    The docker provider does exactly that, so listing both candidates
    unconditionally renders "at X or X" -- and this text is quoted verbatim
    behind the recovery card's "Error details", where a path repeated back to the
    user reads as two places checked when only one was.
    """
    key_path = tmp_path / "ssh_key"
    key_path.write_text("irrelevant-key-material")
    sibling_path = key_path.parent / "known_hosts"
    ssh_info = RemoteSSHInfo(
        user="root", host="203.0.113.5", port=22, key_path=key_path, known_hosts_path=sibling_path
    )

    with pytest.raises(SSHTunnelError) as exc_info:
        _create_ssh_client(ssh_info)
    assert str(exc_info.value).count(str(sibling_path)) == 1


def test_resolve_known_hosts_path_prefers_the_explicit_path_when_it_exists(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "ssh_key"
    key_path.parent.mkdir()
    key_path.write_text("irrelevant-key-material")
    sibling_path = key_path.parent / "known_hosts"
    sibling_path.write_text("sibling-pin")
    explicit_path = tmp_path / "pins" / "known_hosts"
    explicit_path.parent.mkdir()
    explicit_path.write_text("explicit-pin")
    ssh_info = RemoteSSHInfo(
        user="root", host="203.0.113.5", port=22, key_path=key_path, known_hosts_path=explicit_path
    )

    assert _resolve_known_hosts_path(ssh_info) == explicit_path


def test_resolve_known_hosts_path_falls_back_to_the_key_sibling_when_explicit_is_missing(tmp_path: Path) -> None:
    """A stale producer path must never break a connection the sibling convention would have allowed."""
    key_path = tmp_path / "keys" / "ssh_key"
    key_path.parent.mkdir()
    key_path.write_text("irrelevant-key-material")
    sibling_path = key_path.parent / "known_hosts"
    sibling_path.write_text("sibling-pin")
    missing_explicit_path = tmp_path / "gone" / "known_hosts"
    ssh_info = RemoteSSHInfo(
        user="root", host="203.0.113.5", port=22, key_path=key_path, known_hosts_path=missing_explicit_path
    )

    assert _resolve_known_hosts_path(ssh_info) == sibling_path


def test_resolve_known_hosts_path_uses_the_user_known_hosts_for_credential_deferring_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty key_path defers credentials to the user's SSH setup, so their known_hosts is the pin source."""
    home = tmp_path / "home"
    user_known_hosts = home / ".ssh" / "known_hosts"
    user_known_hosts.parent.mkdir(parents=True)
    user_known_hosts.write_text("user-pin")
    monkeypatch.setenv("HOME", str(home))
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=Path(""))

    assert _resolve_known_hosts_path(ssh_info) == user_known_hosts


def test_resolve_known_hosts_path_returns_none_for_credential_deferring_host_without_user_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user known_hosts either: still no candidate (the client then refuses rather than TOFU)."""
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=Path(""))

    assert _resolve_known_hosts_path(ssh_info) is None


def test_create_ssh_client_refusal_names_the_user_known_hosts_for_credential_deferring_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-key refusal points at the user's known_hosts, not a cwd-relative sibling."""
    home = tmp_path / "empty-home"
    monkeypatch.setenv("HOME", str(home))
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=Path(""))

    with pytest.raises(SSHTunnelError, match=str(home / ".ssh" / "known_hosts")):
        _create_ssh_client(ssh_info)


def test_resolve_known_hosts_path_returns_none_when_no_candidate_exists(tmp_path: Path) -> None:
    key_path = tmp_path / "ssh_key"
    key_path.write_text("irrelevant-key-material")
    ssh_info = RemoteSSHInfo(user="root", host="203.0.113.5", port=22, key_path=key_path)

    assert _resolve_known_hosts_path(ssh_info) is None


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
    _setup_raise: Exception | None = PrivateAttr(default=None)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        remote_port: int = 0,
        agent_id: str | None = None,
    ) -> int:
        self._setup_calls.append((ssh_info, local_port, remote_port, agent_id))
        if self._setup_raise is not None:
            raise self._setup_raise
        return self._setup_port


def _make_fake_reverse_tunnel_manager(
    remote_port: int = 9999,
    raise_on_setup: Exception | None = None,
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
    manager = _make_fake_reverse_tunnel_manager(
        raise_on_setup=SSHTunnelError("simulated failure", SSHTunnelPhase.HOST_CONNECT)
    )
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
    manager = _make_fake_reverse_tunnel_manager(
        raise_on_setup=SSHTunnelError("simulated failure", SSHTunnelPhase.HOST_CONNECT)
    )
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
    manager = _make_fake_reverse_tunnel_manager(
        raise_on_setup=SSHTunnelError("simulated failure", SSHTunnelPhase.HOST_CONNECT)
    )
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
        manager._record_repair_failure(
            tunnel_key, conn_key, tunnel_info, SSHTunnelError("x", SSHTunnelPhase.HOST_CONNECT)
        )

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
    manager._record_repair_failure(tunnel_key, conn_key, tunnel_info, SSHTunnelError("x", SSHTunnelPhase.HOST_CONNECT))
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
    manager._record_repair_failure(tunnel_key, conn_key, tunnel_info, SSHTunnelError("x", SSHTunnelPhase.HOST_CONNECT))
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


def test_remove_reverse_tunnel_drops_only_the_named_endpoint(tmp_path: Path) -> None:
    """Removing one endpoint's tunnel leaves a same-agent tunnel to another endpoint intact.

    The latchkey discovery handler clears a stale desktop->container tunnel on
    every discovery cycle while the same agent's desktop->VPS tunnel must stay
    up; an agent-keyed removal would tear down (and force a re-dial of) both.
    """
    container_ssh_info = _sample_ssh_info(tmp_path)
    vps_ssh_info = RemoteSSHInfo(
        user="root",
        host="198.51.100.7",
        port=22,
        key_path=tmp_path / "vps-key",
    )
    container_client = FakeSSHClient.create(active=True)
    vps_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(container_ssh_info, container_client)
    container_conn_key = f"{container_ssh_info.host}:{container_ssh_info.port}"
    vps_conn_key = f"{vps_ssh_info.host}:{vps_ssh_info.port}"
    with manager._lock:
        manager._connections[vps_conn_key] = vps_client
        manager._reverse_tunnels[(container_conn_key, 8420)] = ReverseTunnelInfo(
            ssh_info=container_ssh_info,
            local_port=8420,
            remote_port=1989,
            requested_remote_port=1989,
            agent_id="agent-a@host-1",
        )
        manager._reverse_tunnels[(vps_conn_key, 8420)] = ReverseTunnelInfo(
            ssh_info=vps_ssh_info,
            local_port=8420,
            remote_port=1988,
            requested_remote_port=1988,
            agent_id="agent-a@host-1",
        )

    # Removing an endpoint with no registered tunnel reports nothing removed.
    assert manager.remove_reverse_tunnel(container_ssh_info, 9999) is False

    assert manager.remove_reverse_tunnel(container_ssh_info, 8420) is True
    with manager._lock:
        assert (container_conn_key, 8420) not in manager._reverse_tunnels
        # The same agent's tunnel to the other endpoint (and its SSH
        # connection) survives.
        assert (vps_conn_key, 8420) in manager._reverse_tunnels
        assert vps_conn_key in manager._connections

    # A repeat removal of the already-removed endpoint is a no-op.
    assert manager.remove_reverse_tunnel(container_ssh_info, 8420) is False
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


# -- Forward-tunnel channel opening ----------------------------------------
#
# A transport that has silently gone away keeps reporting ``is_active() ==
# True``, so the only thing distinguishing it from a healthy one is that
# opening a channel never completes.


class _OpenChannelRecorder:
    """Transport stand-in that records ``open_channel`` calls and raises a chosen error.

    ``blocker``, when set, is waited on before the call returns or raises --
    letting a test hold an open in flight while it asserts that a second
    connection is still served.
    """

    _error: Exception | None
    _calls: list[dict[str, object]]
    _entered: threading.Semaphore
    _blocker: threading.Event | None
    _active: bool

    @classmethod
    def create(
        cls,
        error: Exception | None = None,
        blocker: threading.Event | None = None,
        active: bool = True,
    ) -> "_OpenChannelRecorder":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_error", error)
        object.__setattr__(instance, "_calls", [])
        object.__setattr__(instance, "_entered", threading.Semaphore(0))
        object.__setattr__(instance, "_blocker", blocker)
        object.__setattr__(instance, "_active", active)
        return instance

    def open_channel(
        self,
        kind: str,
        dest_addr: tuple[str, int] | None = None,
        src_addr: tuple[str, int] | None = None,
        window_size: int | None = None,
        max_packet_size: int | None = None,
        timeout: float | None = None,
    ) -> paramiko.Channel:
        self._calls.append({"kind": kind, "dest_addr": dest_addr, "timeout": timeout})
        self._entered.release()
        if self._blocker is not None:
            self._blocker.wait(timeout=_BLOCKED_OPEN_HOLD_SECONDS)
        raise (
            self._error
            if self._error is not None
            else SSHTunnelError("no channel configured", SSHTunnelPhase.HOST_CONNECT)
        )

    def is_active(self) -> bool:
        return self._active

    def wait_for_calls(self, count: int, timeout: float = 5.0) -> bool:
        """Block until ``open_channel`` has been entered ``count`` times."""
        deadline = time.monotonic() + timeout
        for _ in range(count):
            if not self._entered.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return False
        return True


@contextmanager
def _accepted_connection() -> Iterator[tuple[socket.socket, socket.socket]]:
    """A connected AF_UNIX pair, standing in for an accepted tunnel connection and its peer.

    Yields ``(client_sock, peer)``: ``client_sock`` is what an accept loop would
    hand to ``_open_and_relay``, and ``peer`` is the end the proxy holds, so a
    test can observe what the proxy observes. Both are closed on exit;
    ``_open_and_relay`` closes the end it is handed, and ``close`` is idempotent.
    """
    client_sock, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        yield client_sock, peer
    finally:
        client_sock.close()
        peer.close()


@contextmanager
def _short_path_tmpdir() -> Iterator[Path]:
    """A temp dir for test sockets, built by the same rule the manager uses for its own.

    pytest's ``tmp_path`` will not do: on macOS it is under /var/folders/... and
    overflows AF_UNIX's sun_path limit on its own.
    """
    with _create_short_path_tmpdir("mngr-fwd-test-") as tmpdir:
        yield Path(tmpdir)


@contextmanager
def _running_accept_loop(transport: _OpenChannelRecorder) -> Iterator[tuple[Path, threading.Event, threading.Thread]]:
    """Run a tunnel accept loop on a listening socket, and tear it down afterwards."""
    shutdown_event = threading.Event()
    stop_event = threading.Event()
    with _short_path_tmpdir() as tmpdir:
        socket_path = tmpdir / "t.sock"
        # Listening before the loop starts, exactly as the manager does it, so
        # the socket is connectable as soon as this returns.
        server = _create_tunnel_listener(socket_path)
        loop = threading.Thread(
            target=_tunnel_accept_loop,
            args=(
                server,
                socket_path,
                cast(paramiko.Transport, transport),
                "127.0.0.1",
                8000,
                shutdown_event,
                stop_event,
                lambda: None,
                lambda: None,
            ),
            daemon=True,
        )
        loop.start()
        try:
            yield socket_path, stop_event, loop
        finally:
            shutdown_event.set()
            loop.join(timeout=5.0)


@pytest.mark.parametrize(
    "is_active, open_seconds, expected",
    [
        # sshd answered, one round trip: the connection is fine either way,
        # whether the refusal kept its ChannelException or not.
        (True, 0.01, False),
        # Ran out the bound while paramiko still called the transport active:
        # the post-sleep half-open case this bound exists for.
        (True, _CHANNEL_OPEN_TIMEOUT_SECONDS, True),
        # Paramiko noticed the peer go away on its own.
        (False, 0.01, True),
    ],
)
def test_transport_is_unusable_only_when_the_peer_stopped_answering(
    is_active: bool, open_seconds: float, expected: bool
) -> None:
    """Which failed opens mean the SSH connection itself must be dropped.

    Covers every way ``Transport.open_channel`` can fail. The exception type is
    deliberately not consulted, because paramiko cannot make it reliable -- see
    ``test_only_a_transport_level_open_failure_invalidates_the_connection``.
    """
    transport = _OpenChannelRecorder.create(active=is_active)
    assert _is_transport_unusable(cast(paramiko.Transport, transport), open_seconds) is expected


def test_channel_open_carries_the_configured_bound() -> None:
    """The open must not fall back to paramiko's 3600s default channel timeout.

    That default is what wedged the tunnel for the rest of the session: an open
    against a peer that silently went away blocked for an hour.
    """
    transport = _OpenChannelRecorder.create(error=paramiko.ChannelException(2, "Connect failed"))

    with _accepted_connection() as (client_sock, _peer):
        _open_and_relay(
            client_sock, cast(paramiko.Transport, transport), "127.0.0.1", 8000, lambda: None, lambda: None
        )

    assert transport._calls[0]["timeout"] == _CHANNEL_OPEN_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "error, is_active, expected_invalidated",
    [
        # sshd answered and refused: a workspace whose system interface has not
        # started listening yet refuses every open until it comes up. Dropping
        # the SSH connection on each one would churn a healthy connection --
        # and every reverse tunnel sharing it -- through a normal cold boot.
        (paramiko.ChannelException(2, "Connect failed"), True, False),
        # The same refusal, arriving as a bare SSHException because it lost the
        # race for ``saved_exception``. A page load fans out several parallel
        # requests, so these are the common case, not the rare one.
        (paramiko.SSHException("Unable to open channel."), True, False),
        # Paramiko noticed the peer go away on its own.
        (paramiko.SSHException("SSH session not active"), False, True),
        # An open that was in flight when the peer closed under it. Closing a
        # shared SSH client is itself one way a sibling tunnel's open lands here.
        (EOFError(), False, True),
    ],
)
def test_only_a_transport_level_open_failure_invalidates_the_connection(
    error: Exception, is_active: bool, expected_invalidated: bool
) -> None:
    """Which failed opens retire the SSH connection, across every exception an open can raise.

    The exception type is deliberately not consulted, because paramiko cannot
    make it reliable: ``Transport.saved_exception`` is one slot shared by every
    in-flight open and ``get_exception`` clears it, so when sshd refuses a burst
    of opens only the first waiter to wake sees the ``ChannelException``; the
    rest get a bare ``SSHException``, the same type a genuine transport failure
    raises. Measured against a real paramiko transport, two opens refused in one
    burst split one and one.

    ``EOFError`` is in the set because paramiko re-raises a bare one from an open
    the peer closed under; it is neither an ``SSHException`` nor an ``OSError``,
    so letting it escape the relay thread would leave the accepted socket open
    and never retire the tunnel.

    Every case must also close the accepted socket, whatever the verdict: that
    close is what makes the proxy see the failure immediately instead of waiting
    out its own timeout.
    """
    transport = _OpenChannelRecorder.create(error=error, active=is_active)
    invalidated = threading.Event()
    refused = threading.Event()

    with _accepted_connection() as (client_sock, peer):
        _open_and_relay(
            client_sock,
            cast(paramiko.Transport, transport),
            "127.0.0.1",
            8000,
            invalidated.set,
            refused.set,
        )

        assert invalidated.is_set() is expected_invalidated
        # The two reports are exclusive: an open that failed is evidence about
        # the inner port or about the SSH connection, never about both. The
        # refusal report is the only trace of a reachable host with nothing
        # listening -- all the proxy sees is the socket closing below.
        assert refused.is_set() is (not expected_invalidated)
        # An empty read means our end is closed.
        peer.settimeout(5.0)
        assert peer.recv(1) == b""


def test_transport_failure_handler_retires_the_connection_and_the_loop(tmp_path: Path) -> None:
    """The handler acts on the live manager, not a copy, and retires the tunnel with it.

    Guards the seam between the two: the handler is a pydantic model holding
    the manager, so a model that copied its ``manager`` on construction would
    invalidate a detached clone and leave the real cache untouched -- a no-op
    that nothing else in the suite would notice.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)
    stop_event = threading.Event()

    handler = _TransportFailureHandler(
        manager=manager,
        conn_key=conn_key,
        client=fake_client,
        stop_event=stop_event,
    )
    handler()

    assert conn_key not in manager._connections
    assert stop_event.is_set()


def test_invalidate_connection_drops_the_cached_client(tmp_path: Path) -> None:
    """Invalidating removes the client from the cache so the next request reconnects."""
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager._invalidate_connection(conn_key, fake_client)

    assert conn_key not in manager._connections


def test_invalidate_connection_leaves_a_replacement_alone(tmp_path: Path) -> None:
    """A late invalidation from a stale connection must not drop its replacement.

    Several requests can be in flight against the same dead transport and all
    time out. The first invalidation reconnects; the rest must be no-ops
    rather than tearing down the fresh connection.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    stale_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, stale_client)

    replacement = FakeSSHClient.create(active=True)
    with manager._lock:
        manager._connections[conn_key] = replacement

    manager._invalidate_connection(conn_key, stale_client)

    assert manager._connections[conn_key] is replacement


def test_accept_loop_serves_a_second_connection_while_an_open_is_stuck() -> None:
    """A wedged channel open must not queue every later connection behind it.

    The regression this guards: opening the channel on the accept loop itself
    meant one stuck open (an hour, at paramiko's default timeout) blocked the
    whole tunnel, so nothing got through and nothing detected it.
    """
    blocker = threading.Event()
    transport = _OpenChannelRecorder.create(
        error=paramiko.SSHException("Timeout opening channel."),
        blocker=blocker,
    )

    try:
        with _running_accept_loop(transport) as (socket_path, _stop_event, _loop):
            first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                first.connect(str(socket_path))
                second.connect(str(socket_path))
                # Both opens must be in flight at once. With the open on the
                # accept loop, the second is never reached while the first
                # is blocked.
                assert transport.wait_for_calls(2)
            finally:
                first.close()
                second.close()
    finally:
        blocker.set()


def test_accept_loop_stops_when_its_tunnel_is_invalidated() -> None:
    """Setting the per-tunnel stop event retires the loop so the next request rebuilds it."""
    transport = _OpenChannelRecorder.create()

    with _running_accept_loop(transport) as (socket_path, stop_event, loop):
        stop_event.set()
        loop.join(timeout=5.0)
        assert not loop.is_alive()
        assert not socket_path.exists()


def test_tunnel_listener_is_listening_before_it_is_returned() -> None:
    """The listener is connectable the instant it is handed back, not merely bound.

    The socket file appears at ``bind()``, but connections are only accepted
    after ``listen()``. A caller that waited for the *file* could land in
    between and be refused, which reads as an unreachable backend.

    Driven against ``_create_tunnel_listener`` directly rather than through
    ``get_tunnel_socket_path``: going through the manager puts a thread start
    and two dict writes between the listen and the assertion, which is ample
    for a background thread to have run ``listen()`` on its own. Moving the
    listen back out of this function passes that version and fails this one.
    """
    with _short_path_tmpdir() as tmpdir:
        socket_path = tmpdir / "t.sock"
        server = _create_tunnel_listener(socket_path)
        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert client_sock.connect_ex(str(socket_path)) == 0
        finally:
            client_sock.close()
            server.close()


def test_tunnel_listener_bind_failure_leaves_the_existing_socket_alone() -> None:
    """A path this call did not bind is not ours to unlink, even though a later failure would.

    ``bind`` creates the socket file and closing the socket does not remove it,
    so a listener that fails *after* binding has to unlink. One that fails at
    the bind must not: tunnel socket paths are a deterministic hash of the
    tunnel key, so the thing already bound there is another listener, and
    unlinking leaves it accepting on an inode no caller can reach.
    """
    with _short_path_tmpdir() as tmpdir:
        socket_path = tmpdir / "t.sock"
        incumbent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            incumbent.bind(str(socket_path))
            incumbent.listen(1)

            # A socket this device could not bind is raised against this
            # device's own socket table, so it carries the same phase.
            with pytest.raises(SSHTunnelError) as exc_info:
                _create_tunnel_listener(socket_path)
            assert exc_info.value.phase is SSHTunnelPhase.LOCAL_SETUP

            # Connectable, not merely present: a path that was unlinked and
            # re-created would still pass an ``exists()`` check.
            client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                assert client_sock.connect_ex(str(socket_path)) == 0
            finally:
                client_sock.close()
        finally:
            incumbent.close()


def _connect_and_read_until_closed(socket_path: Path) -> None:
    """Drive one connection through a tunnel and wait for the tunnel to close it.

    Both failing open paths close the accepted socket, so an empty read is the
    signal that the open was attempted and decided upon.
    """
    client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client_sock.connect(str(socket_path))
        client_sock.settimeout(5.0)
        assert client_sock.recv(1) == b""
    finally:
        client_sock.close()


def test_a_tunnel_over_a_dead_transport_is_rebuilt_on_the_next_request(tmp_path: Path) -> None:
    """The whole recovery, driven through the manager: refuse, retire, rebuild.

    The pieces are covered individually above; this is the wiring that turns
    them into a recovery, driven through ``get_tunnel_socket_path``: it holds
    the accept loop to the per-tunnel stop event, the failure handler to the
    cached client, and the rebuilt tunnel to the same socket path the retired
    one unlinked.
    """
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    tunnel_key = f"{conn_key}->127.0.0.1:8000"
    dying_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, dying_client)

    try:
        socket_path = manager.get_tunnel_socket_path(ssh_info, "127.0.0.1", 8000)
        original_thread = manager._tunnel_threads[tunnel_key]

        # sshd is answering and refusing: a workspace whose service has not
        # come up yet. Neither the connection nor the tunnel may be retired,
        # or a normal cold boot would churn both on every request.
        _connect_and_read_until_closed(socket_path)
        assert manager._connections[conn_key] is dying_client
        assert original_thread.is_alive()

        # Now the peer goes away.
        dying_client._fake_transport.set_active(False)
        _connect_and_read_until_closed(socket_path)
        assert poll_until(lambda: conn_key not in manager._connections)
        assert poll_until(lambda: not original_thread.is_alive())

        # The next request establishes both fresh. The path is a hash of the
        # tunnel key, so the rebuilt tunnel has to reclaim the very path the
        # retired accept loop just unlinked.
        replacement_client = FakeSSHClient.create(active=True)
        with manager._lock:
            manager._connections[conn_key] = replacement_client

        rebuilt_path = manager.get_tunnel_socket_path(ssh_info, "127.0.0.1", 8000)

        assert rebuilt_path == socket_path
        assert manager._tunnel_threads[tunnel_key] is not original_thread
        _connect_and_read_until_closed(rebuilt_path)
        assert manager._connections[conn_key] is replacement_client
    finally:
        manager.cleanup()


def test_reading_the_refusal_count_does_not_wait_on_a_tunnel_being_established(tmp_path: Path) -> None:
    """The refusal count must be readable while another tunnel is mid-setup.

    The proxy reads it on its event loop, once per request, to tell a refused
    inner port from an unreachable host. ``_lock`` is held for the whole of
    ``get_tunnel_socket_path`` -- including a ``paramiko`` connect that runs to
    its 10s timeout against a host that has gone away, which is exactly the
    situation this count exists to classify. Sharing that lock would park every
    request the proxy is serving behind it, so the counter keeps its own.
    """
    manager = SSHTunnelManager()
    ssh_info = _sample_ssh_info(tmp_path)
    manager._record_backend_refusal(f"{ssh_info.host}:{ssh_info.port}->127.0.0.1:8000")

    with manager._lock:
        read_count: list[int] = []
        reader = threading.Thread(
            target=lambda: read_count.append(manager.get_backend_refusal_count(ssh_info, "127.0.0.1", 8000))
        )
        reader.start()
        reader.join(timeout=5.0)
        assert not reader.is_alive(), "get_backend_refusal_count blocked on the tunnel manager's setup lock"

    assert read_count == [1]


# -- Refusal classification against a real sshd ----------------------------
#
# ``BACKEND_NOT_LISTENING`` rests on one judgement no fake can make for it:
# whether a real sshd refusing a ``direct-tcpip`` open leaves the transport in
# the state ``_is_transport_unusable`` reads as still usable. Every test above
# supplies that state itself, so all of them would keep passing if the real
# thing landed on the other side of the line -- and the refusal count would then
# never move, silently, with the reason simply never firing. These two run the
# real client against a real server so the judgement is made rather than
# assumed. The seam above them (a count that moved becoming the reason on the
# envelope) is covered in ``server_test.py``.

# How long the fake sshd's accept loop waits before re-checking for shutdown.
_SSHD_ACCEPT_POLL_SECONDS: Final[float] = 0.2


class _InnerPortRefusingServer(paramiko.ServerInterface):
    """An sshd that admits anyone and refuses every forward to a port behind it.

    The shape of a live container whose service has died: the host answers, its
    transport stays healthy, and nothing is listening on the inner port.
    """

    def get_allowed_auths(self, username: str) -> str:
        return "publickey"

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        return AUTH_SUCCESSFUL

    def check_channel_direct_tcpip_request(
        self, chanid: int, origin: tuple[str, int], destination: tuple[str, int]
    ) -> int:
        return OPEN_FAILED_CONNECT_FAILED


@contextmanager
def _refusing_sshd(tmp_path: Path) -> Iterator[tuple[RemoteSSHInfo, list[paramiko.Transport]]]:
    """Run an sshd on loopback that refuses every ``direct-tcpip`` open.

    Yields the ``RemoteSSHInfo`` that addresses it -- real key material, and a
    known_hosts pinning this server's key, so ``_create_ssh_client`` performs a
    real handshake rather than being handed a connection -- along with the
    server-side transports it has accepted, which a caller closes to make the
    host go away mid-test.
    """
    host_key = paramiko.ECDSAKey.generate()
    key_path = tmp_path / "id_ecdsa"
    paramiko.ECDSAKey.generate().write_private_key_file(str(key_path))

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(_SSHD_ACCEPT_POLL_SECONDS)
    port = listener.getsockname()[1]
    # Pinned under OpenSSH's non-default-port spelling, which is what paramiko
    # looks the host up by. Written beside the key, the placement
    # ``_resolve_known_hosts_path`` falls back to.
    (tmp_path / "known_hosts").write_text(f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n")

    served: list[paramiko.Transport] = []
    stop_event = threading.Event()

    def accept_loop() -> None:
        while not stop_event.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            transport = paramiko.Transport(connection)
            transport.add_server_key(host_key)
            transport.start_server(server=_InnerPortRefusingServer())
            served.append(transport)

    thread = threading.Thread(target=accept_loop, daemon=True, name="test-refusing-sshd")
    thread.start()
    try:
        yield RemoteSSHInfo(user="root", host="127.0.0.1", port=port, key_path=key_path), served
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        for transport in served:
            transport.close()
        listener.close()


@pytest.mark.timeout(60)
def test_a_real_sshd_refusing_the_inner_port_counts_a_refusal_and_keeps_the_connection(tmp_path: Path) -> None:
    """A refused ``direct-tcpip`` open must be counted, and must not retire the SSH connection.

    The two halves are one judgement. The refusal is only tellable from an
    unreachable host because the transport survives it, so a run that retired
    the connection would also have counted nothing -- and every request against
    a container with a dead service would rebuild the tunnel to learn the same
    thing again.

    The count is read straight after the failed request rather than polled for,
    because the ordering is the contract: the refusal is recorded before the
    accepted socket is closed, precisely so a caller that has observed its own
    failure is guaranteed to see it. A poll here would pass either way.
    """
    with _refusing_sshd(tmp_path) as (ssh_info, _served):
        manager = SSHTunnelManager()
        try:
            socket_path = manager.get_tunnel_socket_path(ssh_info, "127.0.0.1", 8000)
            tunnel_key = f"{ssh_info.host}:{ssh_info.port}->127.0.0.1:8000"
            assert manager.get_backend_refusal_count(ssh_info, "127.0.0.1", 8000) == 0

            _connect_and_read_until_closed(socket_path)

            assert manager.get_backend_refusal_count(ssh_info, "127.0.0.1", 8000) == 1
            assert f"{ssh_info.host}:{ssh_info.port}" in manager._connections
            assert manager._tunnel_threads[tunnel_key].is_alive()
        finally:
            manager.cleanup()


@pytest.mark.timeout(60)
def test_a_real_ssh_host_that_goes_away_counts_no_refusal(tmp_path: Path) -> None:
    """A host that stopped answering must not be read as a refused inner port.

    The negative half, and the reason the count cannot simply be "an open
    failed": both failures reach the proxy as nothing but the tunnel socket
    closing. Here the same server, refusing the same way, has had its transport
    taken out from under it -- and that alone has to flip the classification, or
    a vanished machine would be reported as reachable-with-a-dead-service and
    the restart that fixes it withheld.
    """
    with _refusing_sshd(tmp_path) as (ssh_info, served):
        manager = SSHTunnelManager()
        conn_key = f"{ssh_info.host}:{ssh_info.port}"
        try:
            socket_path = manager.get_tunnel_socket_path(ssh_info, "127.0.0.1", 8000)
            assert poll_until(lambda: len(served) == 1), "the sshd never accepted the manager's connection"

            served[0].close()
            client_transport = manager._connections[conn_key].get_transport()
            assert client_transport is not None
            assert poll_until(lambda: not client_transport.is_active()), "the client never noticed the host go away"

            _connect_and_read_until_closed(socket_path)

            assert poll_until(lambda: conn_key not in manager._connections), "the dead connection was not retired"
            assert manager.get_backend_refusal_count(ssh_info, "127.0.0.1", 8000) == 0
        finally:
            manager.cleanup()
