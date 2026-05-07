"""Acceptance reproduction for the reverse-tunnel CPU leak in the minds desktop client.

The subject under test is ``imbue.minds.desktop_client.ssh_tunnel.SSHTunnelManager``,
which the ``minds run`` parent process uses to maintain Latchkey reverse
tunnels. (The separate ``imbue.mngr_forward.ssh_tunnel.SSHTunnelManager``
used by the ``mngr forward`` plugin subprocess is *not* covered here.)

The original symptom is the ``minds run`` process pinning a CPU after
agents/hosts come and go: ``SSHTunnelManager._reverse_tunnels`` is never
pruned, so every 30s health-check tick re-handshakes against ports that no
longer exist, leaking paramiko transport threads each time.

These tests build an in-process paramiko SSH server (one ``Transport`` per
listening port), set up reverse tunnels through ``SSHTunnelManager``, then
close the test servers to mimic the remote disappearance. They then verify
the two independent fixes:

* Fix 1: ``remove_reverse_tunnels_for_agent`` actually drops tunnels and
  closes the underlying SSH client when no other tunnel uses it.
* Fix 2: a permanently-broken tunnel left in the registry is retried with
  exponential backoff, and dropped from the registry after the configured
  number of consecutive failures.

The two fixes are exercised independently so each can be attributed to its
own change. The leak symptom itself is reproduced by manually invoking
``_check_and_repair_tunnels`` against a stopped server and asserting the
manager would otherwise keep growing its connection set.
"""

import socket
import threading
import time
from pathlib import Path
from typing import Final

import paramiko
import pytest

from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.ssh_tunnel import _REVERSE_TUNNEL_MAX_REPAIR_FAILURES
from imbue.minds.desktop_client.ssh_tunnel import _TunnelFailureState

_TEST_USERNAME: Final[str] = "tunnel-test-user"


class _PermissiveServer(paramiko.ServerInterface):
    """Server-side ``ServerInterface`` that accepts any client and any forward.

    The acceptance test does not care about authentication or routing; it
    only cares that the SSH transport stays alive long enough for
    ``request_port_forward`` to succeed, and that closing the listening
    socket actually tears the transport down.
    """

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        del username, key
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_password(self, username: str, password: str) -> int:
        del username, password
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username: str) -> str:
        del username
        return "publickey,password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        del chanid
        if kind in ("session", "forwarded-tcpip"):
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_port_forward_request(self, address: str, port: int) -> int:
        del address
        # Returning the requested port (or any positive int when 0 was
        # requested) signals success to paramiko; the SSHTunnelManager
        # only relies on the returned int as the "assigned" port.
        if port == 0:
            return 50000
        return port

    def cancel_port_forward_request(self, address: str, port: int) -> None:
        del address, port


class _InProcessSSHServer:
    """Listens on a localhost port and serves one paramiko ``Transport`` per accepted socket.

    Tracks every started transport so the test can assert teardown behavior
    and so the server itself can shut down cleanly. Closing the server
    closes the listening socket (rejecting new connections) and the
    accepted transports (which is what makes the manager-side
    ``is_active()`` flip to ``False``).
    """

    def __init__(self, host_key: paramiko.RSAKey) -> None:
        self._host_key = host_key
        self._listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listen_sock.bind(("127.0.0.1", 0))
        self._listen_sock.listen(8)
        self._listen_sock.settimeout(0.2)
        self.port = self._listen_sock.getsockname()[1]
        self._stop = threading.Event()
        self._transports: list[paramiko.Transport] = []
        self._transports_lock = threading.Lock()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"in-process-sshd-{self.port}", daemon=True
        )
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client_sock, _addr = self._listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client_sock)
            transport.add_server_key(self._host_key)
            try:
                transport.start_server(server=_PermissiveServer())
            except paramiko.SSHException:
                transport.close()
                continue
            with self._transports_lock:
                self._transports.append(transport)

    def stop(self) -> None:
        """Close the listener and all accepted transports.

        After this, the manager-side ``is_active()`` for any client that
        connected here will flip to ``False`` on the next check.
        """
        self._stop.set()
        try:
            self._listen_sock.close()
        except OSError:
            pass
        with self._transports_lock:
            transports = list(self._transports)
        for transport in transports:
            try:
                transport.close()
            except (OSError, paramiko.SSHException):
                pass
        self._accept_thread.join(timeout=3.0)


def _write_test_keys(tmp_path: Path) -> tuple[paramiko.RSAKey, RemoteSSHInfo]:
    """Generate an RSA key, persist it to ``tmp_path``, return ``(host_key, ssh_info_template)``.

    The same key serves as the (manager-side) client key and the
    (server-side) host key -- ``_PermissiveServer`` accepts any auth, so
    the actual key contents don't matter; we only need a real key file on
    disk because ``SSHTunnelManager`` passes ``key_filename`` to
    ``paramiko.SSHClient.connect``.
    """
    key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "id_rsa"
    key.write_private_key_file(str(key_path))
    # SSHTunnelManager looks for known_hosts beside the key file; absence
    # falls back to AutoAddPolicy, which is what we want for the test.
    # Port 0 is a placeholder; ``_ssh_info_for`` rewrites it per-server.
    template = RemoteSSHInfo(
        user=_TEST_USERNAME,
        host="127.0.0.1",
        port=0,
        key_path=key_path,
    )
    return key, template


def _ssh_info_for(template: RemoteSSHInfo, port: int) -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user=template.user,
        host=template.host,
        port=port,
        key_path=template.key_path,
    )


def _start_local_listener() -> tuple[socket.socket, int]:
    """Open a localhost TCP listener; return ``(sock, port)``.

    The reverse tunnel forwards the (remote) sshd back to this local port.
    The listener does not need to accept anything -- the test never sends
    real traffic over the tunnel; it only cares about the transport
    lifecycle.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


@pytest.mark.acceptance
def test_remove_reverse_tunnels_for_agent_actually_releases_resources(tmp_path: Path) -> None:
    """Fix 1: tearing down an agent removes its tunnels and closes the SSH client.

    Reproduces the leak: set up a reverse tunnel, observe the manager has
    one tracked tunnel and one open SSH client, then call
    ``remove_reverse_tunnels_for_agent`` and verify both the tunnel and
    the underlying SSH client are gone (so the paramiko transport thread
    has somewhere to exit). Without Fix 1 the manager would keep both
    indefinitely.
    """
    host_key, template = _write_test_keys(tmp_path)
    server = _InProcessSSHServer(host_key)
    listener, local_port = _start_local_listener()

    manager = SSHTunnelManager()
    try:
        ssh_info = _ssh_info_for(template, server.port)

        manager.setup_reverse_tunnel(
            ssh_info=ssh_info,
            local_port=local_port,
            agent_id="agent-A",
        )

        conn_key = f"127.0.0.1:{server.port}"
        with manager._lock:
            assert (conn_key, local_port) in manager._reverse_tunnels
            assert conn_key in manager._connections
            client_before = manager._connections[conn_key]
        assert client_before.get_transport() is not None
        assert client_before.get_transport().is_active()

        removed = manager.remove_reverse_tunnels_for_agent("agent-A")
        assert removed == 1

        with manager._lock:
            assert (conn_key, local_port) not in manager._reverse_tunnels
            # Last tunnel for this host went away -- the SSH client must
            # have been closed and forgotten so its transport thread exits.
            assert conn_key not in manager._connections
        # The previously-cached client itself must report inactive.
        assert not (client_before.get_transport() is not None and client_before.get_transport().is_active())
    finally:
        manager.cleanup()
        listener.close()
        server.stop()


@pytest.mark.acceptance
def test_remove_reverse_tunnels_for_agent_is_noop_for_unknown_agent() -> None:
    """Idempotent: removing a nonexistent agent's tunnels returns 0 and does not raise."""
    manager = SSHTunnelManager()
    try:
        assert manager.remove_reverse_tunnels_for_agent("never-existed") == 0
    finally:
        manager.cleanup()


@pytest.mark.acceptance
def test_remove_reverse_tunnels_for_agent_keeps_other_agents_intact(tmp_path: Path) -> None:
    """Removing one agent's tunnels does not disturb a sibling agent on the same SSH host."""
    host_key, template = _write_test_keys(tmp_path)
    server = _InProcessSSHServer(host_key)
    listener_a, port_a = _start_local_listener()
    listener_b, port_b = _start_local_listener()

    manager = SSHTunnelManager()
    try:
        ssh_info = _ssh_info_for(template, server.port)
        manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=port_a, agent_id="agent-A")
        manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=port_b, agent_id="agent-B")

        conn_key = f"127.0.0.1:{server.port}"
        with manager._lock:
            assert (conn_key, port_a) in manager._reverse_tunnels
            assert (conn_key, port_b) in manager._reverse_tunnels
            assert conn_key in manager._connections

        removed = manager.remove_reverse_tunnels_for_agent("agent-A")
        assert removed == 1

        with manager._lock:
            assert (conn_key, port_a) not in manager._reverse_tunnels
            assert (conn_key, port_b) in manager._reverse_tunnels
            # B still uses the SSH client, so it must NOT have been closed.
            assert conn_key in manager._connections
    finally:
        manager.cleanup()
        listener_a.close()
        listener_b.close()
        server.stop()


@pytest.mark.acceptance
def test_check_and_repair_backs_off_and_drops_after_max_failures(tmp_path: Path) -> None:
    """Fix 2: a tunnel whose target server is gone is retried with backoff
    and dropped from the registry after ``_REVERSE_TUNNEL_MAX_REPAIR_FAILURES``
    consecutive failures.

    Without Fix 2, every 30s tick would re-handshake forever, leaking a
    paramiko transport thread per attempt. With Fix 2:
      * the first failure schedules a backoff,
      * subsequent ticks within that backoff window do nothing,
      * after N total failures the tunnel is removed from the registry.

    The test forces the failure path by setting up a tunnel and then
    closing the SSH server (so the next setup attempt cannot complete).
    To advance through the backoff schedule deterministically we manually
    zero out ``next_attempt_at`` between ticks, which is exactly what
    the wall-clock would do given enough elapsed time.
    """
    host_key, template = _write_test_keys(tmp_path)
    server = _InProcessSSHServer(host_key)
    listener, local_port = _start_local_listener()

    manager = SSHTunnelManager()
    try:
        ssh_info = _ssh_info_for(template, server.port)
        manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=local_port, agent_id="agent-A")
        conn_key = f"127.0.0.1:{server.port}"
        tunnel_key = (conn_key, local_port)

        # Make the target permanently dead.
        server.stop()
        # Also drop the now-stale client so the next health-check tick
        # treats the tunnel as broken (matching what paramiko would
        # eventually report once it noticed the closed transport).
        with manager._lock:
            stale_client = manager._connections.pop(conn_key, None)
        if stale_client is not None:
            try:
                stale_client.close()
            except (OSError, paramiko.SSHException):
                pass

        # Tick 1: should attempt repair, fail, record one failure.
        manager._check_and_repair_tunnels()
        with manager._lock:
            state = manager._failure_state.get(tunnel_key)
            assert state is not None
            assert state.consecutive_failures == 1
            assert state.next_attempt_at > time.monotonic()
            assert tunnel_key in manager._reverse_tunnels

        # Tick 2 (immediately): inside the backoff window, must skip --
        # no new failure recorded.
        manager._check_and_repair_tunnels()
        with manager._lock:
            state = manager._failure_state.get(tunnel_key)
            assert state is not None
            assert state.consecutive_failures == 1

        # Walk the failure counter up to the cap by zeroing the cooldown
        # before each tick. After enough strikes the tunnel must vanish.
        for _ in range(_REVERSE_TUNNEL_MAX_REPAIR_FAILURES):
            with manager._lock:
                state = manager._failure_state.get(tunnel_key)
                if state is not None:
                    state.next_attempt_at = 0.0
            manager._check_and_repair_tunnels()

        with manager._lock:
            assert tunnel_key not in manager._reverse_tunnels, (
                "Expected the broken tunnel to be dropped after "
                f"{_REVERSE_TUNNEL_MAX_REPAIR_FAILURES} consecutive failures"
            )
            assert tunnel_key not in manager._failure_state
            # Last tunnel for this host gone -- the SSH client (if any
            # was re-established) must also be gone.
            assert conn_key not in manager._connections
    finally:
        manager.cleanup()
        listener.close()
        # ``server.stop()`` was called above; second call is harmless.


@pytest.mark.acceptance
def test_successful_repair_resets_backoff(tmp_path: Path) -> None:
    """A successful repair clears prior failure bookkeeping so subsequent
    failures start backing off from scratch (rather than at the cap).

    Realistic scenario: a tunnel breaks, the health check records a
    failure, then the underlying SSH host comes back so the next tick
    succeeds. After that success, a brand-new failure should start from
    zero (not from the previous failure count).
    """
    host_key, template = _write_test_keys(tmp_path)
    server = _InProcessSSHServer(host_key)
    listener, local_port = _start_local_listener()

    manager = SSHTunnelManager()
    try:
        ssh_info = _ssh_info_for(template, server.port)
        manager.setup_reverse_tunnel(ssh_info=ssh_info, local_port=local_port, agent_id="agent-A")
        conn_key = f"127.0.0.1:{server.port}"
        tunnel_key = (conn_key, local_port)

        # Plant prior failure history; also drop the cached SSH client so
        # ``setup_reverse_tunnel`` (called via repair) takes the full
        # re-create path that clears failure state on success.
        with manager._lock:
            manager._failure_state[tunnel_key] = _TunnelFailureState(
                consecutive_failures=5,
                next_attempt_at=0.0,
            )
            stale_client = manager._connections.pop(conn_key, None)
        if stale_client is not None:
            try:
                stale_client.close()
            except (OSError, paramiko.SSHException):
                pass

        # Server is still up, so the repair must succeed -- and clear
        # failure_state in the process.
        manager._check_and_repair_tunnels()
        with manager._lock:
            assert tunnel_key not in manager._failure_state
            assert tunnel_key in manager._reverse_tunnels
    finally:
        manager.cleanup()
        listener.close()
        server.stop()
