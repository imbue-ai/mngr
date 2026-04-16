import socket
import threading
import time
from pathlib import Path

import paramiko
import pytest
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import ReverseTunnelInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.minds.desktop_client.ssh_tunnel import _relay_data
from imbue.minds.desktop_client.ssh_tunnel import _shell_quote_remote_path
from imbue.minds.desktop_client.ssh_tunnel import _ssh_connection_is_active
from imbue.minds.desktop_client.ssh_tunnel import _ssh_connection_transport
from imbue.minds.desktop_client.ssh_tunnel import _tunnel_accept_loop
from imbue.minds.desktop_client.ssh_tunnel import _wait_for_socket
from imbue.minds.desktop_client.ssh_tunnel import parse_url_host_port
from imbue.mngr.interfaces.ssh_auth import SSHKeyAuth


def _connect_with_retry(sock_path: Path, timeout: float = 10.0) -> socket.socket:
    """Connect to a Unix domain socket, retrying until the server is listening.

    _wait_for_socket only checks file existence, but the server may not be
    listening yet (race between bind and listen). This retries connect until
    it succeeds, then returns the connected socket.
    """
    _wait_for_socket(sock_path, timeout=timeout)
    poll = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(sock_path))
            return client
        except (ConnectionRefusedError, OSError):
            client.close()
            poll.wait(timeout=0.05)
    raise SSHTunnelError(f"Socket {sock_path} exists but not accepting connections after {timeout}s")


class FakeChannelFromSocket:
    """Stub that wraps a real socket to provide a paramiko-Channel-like interface.

    Used in tests to simulate paramiko channels without requiring a real SSH connection.
    """

    _sock: socket.socket

    @classmethod
    def create(cls, sock: socket.socket) -> "FakeChannelFromSocket":
        """Create a FakeChannelFromSocket wrapping the given socket."""
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


class FakeParamikoTransport:
    """Stub for paramiko.Transport that tracks open_channel calls."""

    channel_to_return: object | None
    channel_error: paramiko.SSHException | None
    open_channel_calls: list[tuple[str, tuple[str, int], tuple[str, int]]]

    @classmethod
    def create(cls) -> "FakeParamikoTransport":
        """Create a new FakeParamikoTransport with default values."""
        instance = cls.__new__(cls)
        object.__setattr__(instance, "channel_to_return", None)
        object.__setattr__(instance, "channel_error", None)
        object.__setattr__(instance, "open_channel_calls", [])
        return instance

    def is_active(self) -> bool:
        return True

    def open_channel(
        self,
        kind: str,
        dest_addr: tuple[str, int],
        src_addr: tuple[str, int],
    ) -> object:
        self.open_channel_calls.append((kind, dest_addr, src_addr))
        if self.channel_error is not None:
            raise self.channel_error
        if self.channel_to_return is None:
            raise paramiko.SSHException("No channel configured")
        return self.channel_to_return


class FakeSSHTransport:
    """Minimal stub for paramiko.Transport that reports an active state."""

    _active: bool

    @classmethod
    def create(cls, active: bool = True) -> "FakeSSHTransport":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_active", active)
        return instance

    def is_active(self) -> bool:
        return self._active

    def request_port_forward(self, address: str, port: int) -> int:
        return 54321

    def accept(self, timeout: float | None = None) -> object:
        return None

    def cancel_port_forward(self, address: str, port: int) -> None:
        pass


class _FakeExecChannel:
    """Fake paramiko channel that returns a configurable exit status."""

    def __init__(self, exit_status: int) -> None:
        self._exit_status = exit_status

    def recv_exit_status(self) -> int:
        return self._exit_status

    def close(self) -> None:
        pass


class _FakeExecStream:
    """Fake paramiko stdin/stdout/stderr stream for exec_command results."""

    def __init__(self, channel: _FakeExecChannel) -> None:
        self.channel = channel

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass


class FakeSSHClient(paramiko.SSHClient):
    """Minimal paramiko.SSHClient subclass with a controllable transport for testing.

    Uses __new__ to bypass paramiko SSHClient initialization, injecting only
    the state needed for the methods under test.
    """

    _fake_transport: FakeSSHTransport
    _exec_calls: list[str]
    _exec_exit_status: int
    _exec_raise: type[Exception] | None

    @classmethod
    def create(
        cls,
        active: bool = True,
        exec_exit_status: int = 0,
        exec_raise: type[Exception] | None = None,
    ) -> "FakeSSHClient":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_fake_transport", FakeSSHTransport.create(active=active))
        object.__setattr__(instance, "_exec_calls", [])
        object.__setattr__(instance, "_exec_exit_status", exec_exit_status)
        object.__setattr__(instance, "_exec_raise", exec_raise)
        return instance

    def get_transport(self) -> FakeSSHTransport:
        return self._fake_transport

    def exec_command(
        self,
        command: str,
        bufsize: int = -1,
        timeout: float | None = None,
        get_pty: bool = False,
        environment: object = None,
    ) -> tuple[_FakeExecStream, _FakeExecStream, _FakeExecStream]:
        self._exec_calls.append(command)
        if self._exec_raise is not None:
            raise self._exec_raise("simulated exec error")
        channel = _FakeExecChannel(self._exec_exit_status)
        return _FakeExecStream(channel), _FakeExecStream(channel), _FakeExecStream(channel)

    def close(self) -> None:
        pass


# -- RemoteSSHInfo tests --


def test_remote_ssh_info_constructs_with_valid_fields() -> None:
    info = RemoteSSHInfo(
        user="root",
        host="example.com",
        port=2222,
        auth=SSHKeyAuth(key_path=Path("/tmp/test_key")),
    )
    assert info.user == "root"
    assert info.host == "example.com"
    assert info.port == 2222
    assert info.auth == SSHKeyAuth(key_path=Path("/tmp/test_key"))


def test_remote_ssh_info_is_frozen() -> None:
    info = RemoteSSHInfo(
        user="root",
        host="example.com",
        port=2222,
        auth=SSHKeyAuth(key_path=Path("/tmp/test_key")),
    )
    with pytest.raises(ValidationError):
        info.user = "other"


# -- parse_url_host_port tests --


def test_parse_url_host_port_extracts_host_and_port() -> None:
    host, port = parse_url_host_port("http://127.0.0.1:9100")
    assert host == "127.0.0.1"
    assert port == 9100


def test_parse_url_host_port_defaults_to_port_80_for_http() -> None:
    host, port = parse_url_host_port("http://example.com/path")
    assert host == "example.com"
    assert port == 80


def test_parse_url_host_port_defaults_to_port_443_for_https() -> None:
    host, port = parse_url_host_port("https://example.com/path")
    assert host == "example.com"
    assert port == 443


def test_parse_url_host_port_normalizes_localhost_to_ipv4() -> None:
    host, port = parse_url_host_port("http://localhost:8080")
    assert host == "127.0.0.1"
    assert port == 8080


# -- SSHTunnelManager tests --


def test_tunnel_manager_cleanup_without_tunnels() -> None:
    """Cleanup should work even when no tunnels have been created."""
    manager = SSHTunnelManager()
    manager.cleanup()


def test_tunnel_manager_get_tmpdir_creates_secure_directory() -> None:
    """The temporary directory should have 0o700 permissions."""
    manager = SSHTunnelManager()
    try:
        tmpdir = manager._get_tmpdir()
        assert tmpdir.exists()
        stat = tmpdir.stat()
        assert stat.st_mode & 0o777 == 0o700
    finally:
        manager.cleanup()


def test_tunnel_manager_get_tmpdir_returns_same_path() -> None:
    """Multiple calls to _get_tmpdir return the same directory."""
    manager = SSHTunnelManager()
    try:
        dir1 = manager._get_tmpdir()
        dir2 = manager._get_tmpdir()
        assert dir1 == dir2
    finally:
        manager.cleanup()


def test_wait_for_socket_returns_immediately_when_exists(tmp_path: Path) -> None:
    """_wait_for_socket returns when the socket file already exists."""
    sock_path = tmp_path / "test.sock"
    sock_path.touch()
    _wait_for_socket(sock_path, timeout=5.0)


def test_wait_for_socket_raises_on_timeout(tmp_path: Path) -> None:
    """_wait_for_socket raises SSHTunnelError when the socket does not appear."""
    sock_path = tmp_path / "nonexistent.sock"
    with pytest.raises(SSHTunnelError):
        _wait_for_socket(sock_path, timeout=0.05)


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


# -- _tunnel_accept_loop tests --


def test_tunnel_accept_loop_forwards_connections(short_tmp_path: Path) -> None:
    """The accept loop creates Unix sockets and forwards data through a mock transport."""
    sock_path = short_tmp_path / "test.sock"
    shutdown_event = threading.Event()

    channel_remote, channel_local = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    fake_transport = FakeParamikoTransport.create()
    fake_channel = FakeChannelFromSocket.create(channel_local)
    fake_transport.channel_to_return = fake_channel

    accept_thread = threading.Thread(
        target=_tunnel_accept_loop,
        args=(sock_path, fake_transport, "127.0.0.1", 9100, shutdown_event),
        daemon=True,
    )
    accept_thread.start()

    client = _connect_with_retry(sock_path, timeout=10.0)
    client.settimeout(3.0)
    channel_remote.settimeout(3.0)

    client.sendall(b"test request")
    data = channel_remote.recv(4096)
    assert data == b"test request"

    channel_remote.sendall(b"test response")
    response = client.recv(4096)
    assert response == b"test response"

    client.close()
    channel_remote.close()
    shutdown_event.set()
    accept_thread.join(timeout=5.0)


def test_tunnel_accept_loop_handles_channel_open_failure(short_tmp_path: Path) -> None:
    """When open_channel fails, the accepted client socket is closed gracefully."""
    sock_path = short_tmp_path / "fail.sock"
    shutdown_event = threading.Event()

    fake_transport = FakeParamikoTransport.create()
    fake_transport.channel_error = paramiko.SSHException("Channel denied")

    accept_thread = threading.Thread(
        target=_tunnel_accept_loop,
        args=(sock_path, fake_transport, "127.0.0.1", 9100, shutdown_event),
        daemon=True,
    )
    accept_thread.start()

    client = _connect_with_retry(sock_path, timeout=10.0)
    client.settimeout(3.0)

    try:
        data = client.recv(4096)
        assert data == b""
    except socket.timeout:
        pass

    client.close()
    shutdown_event.set()
    accept_thread.join(timeout=3.0)


def test_tunnel_accept_loop_shutdown_event_stops_loop(short_tmp_path: Path) -> None:
    """Setting the shutdown event causes the accept loop to exit."""
    sock_path = short_tmp_path / "shutdown.sock"
    shutdown_event = threading.Event()

    fake_transport = FakeParamikoTransport.create()

    accept_thread = threading.Thread(
        target=_tunnel_accept_loop,
        args=(sock_path, fake_transport, "127.0.0.1", 9100, shutdown_event),
        daemon=True,
    )
    accept_thread.start()

    _wait_for_socket(sock_path, timeout=10.0)

    shutdown_event.set()
    accept_thread.join(timeout=10.0)
    assert not accept_thread.is_alive()


# -- _shell_quote_remote_path tests --


def test_shell_quote_remote_path_tilde_slash() -> None:
    """Paths starting with ~/ are converted to use $HOME/ for shell expansion."""
    result = _shell_quote_remote_path("~/.mngr/agents/agent-123")
    assert result == '"$HOME/.mngr/agents/agent-123"'


def test_shell_quote_remote_path_tilde_only() -> None:
    """A bare '~' path is converted to $HOME."""
    result = _shell_quote_remote_path("~")
    assert result == '"$HOME"'


def test_shell_quote_remote_path_absolute() -> None:
    """Absolute paths are passed through shlex.quote (safe chars are returned unquoted)."""
    result = _shell_quote_remote_path("/home/user/.mngr/agents/agent-123")
    # Path has only safe chars, shlex.quote leaves it unquoted
    assert result == "/home/user/.mngr/agents/agent-123"


def test_shell_quote_remote_path_plain_name() -> None:
    """Plain names without tilde are shell-quoted normally."""
    result = _shell_quote_remote_path("agents/agent-123")
    assert result == "agents/agent-123"


# -- write_api_url_to_local tests --


def test_write_api_url_to_local_creates_file(tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "test-agent"
    SSHTunnelManager.write_api_url_to_local(state_dir, "http://127.0.0.1:8420")

    url_file = state_dir / "minds_api_url"
    assert url_file.exists()
    assert url_file.read_text() == "http://127.0.0.1:8420"


def test_write_api_url_to_local_creates_parent_dirs(tmp_path: Path) -> None:
    state_dir = tmp_path / "deep" / "nested" / "path"
    SSHTunnelManager.write_api_url_to_local(state_dir, "http://127.0.0.1:9000")

    assert (state_dir / "minds_api_url").read_text() == "http://127.0.0.1:9000"


def test_write_api_url_to_local_overwrites_existing(tmp_path: Path) -> None:
    state_dir = tmp_path / "agents" / "test-agent"
    SSHTunnelManager.write_api_url_to_local(state_dir, "http://127.0.0.1:8420")
    SSHTunnelManager.write_api_url_to_local(state_dir, "http://127.0.0.1:9999")

    assert (state_dir / "minds_api_url").read_text() == "http://127.0.0.1:9999"


def test_reverse_tunnel_info_stores_metadata() -> None:
    ssh_info = RemoteSSHInfo(
        user="root",
        host="192.168.1.1",
        port=22,
        auth=SSHKeyAuth(key_path=Path("/tmp/test_key")),
    )
    info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=54321,
        agent_state_dirs=["~/.mngr/agents/test-id"],
    )
    assert info.local_port == 8420
    assert info.remote_port == 54321
    assert info.agent_state_dirs == ["~/.mngr/agents/test-id"]


def test_tunnel_manager_cleanup_with_no_tunnels() -> None:
    """Verify cleanup works even when no tunnels have been established."""
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


class _FakeSSHTunnelManager(SSHTunnelManager):
    """Test double that overrides setup_reverse_tunnel and write_api_url_to_remote
    so tests can exercise _check_and_repair_tunnels without a real SSH server.
    """

    _setup_calls: list[tuple[RemoteSSHInfo, int, str]] = PrivateAttr(default_factory=list)
    _write_calls: list[tuple[RemoteSSHInfo, str, str]] = PrivateAttr(default_factory=list)
    _setup_port: int = PrivateAttr(default=9999)
    _setup_raise: type[Exception] | None = PrivateAttr(default=None)

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        agent_state_dir: str,
    ) -> int:
        self._setup_calls.append((ssh_info, local_port, agent_state_dir))
        if self._setup_raise is not None:
            raise self._setup_raise("simulated failure")
        return self._setup_port

    def write_api_url_to_remote(
        self,
        ssh_info: RemoteSSHInfo,
        agent_state_dir: str,
        url: str,
    ) -> None:
        self._write_calls.append((ssh_info, agent_state_dir, url))


def _make_fake_reverse_tunnel_manager(
    remote_port: int = 9999,
    raise_on_setup: type[Exception] | None = None,
) -> _FakeSSHTunnelManager:
    """Create a _FakeSSHTunnelManager with the given configuration."""
    mgr = _FakeSSHTunnelManager()
    mgr._setup_port = remote_port
    mgr._setup_raise = raise_on_setup
    return mgr


def _sample_ssh_info(tmp_path: Path) -> RemoteSSHInfo:
    return RemoteSSHInfo(
        user="root",
        host="192.0.2.1",
        port=22,
        auth=SSHKeyAuth(key_path=tmp_path / "key"),
    )


def test_check_and_repair_tunnels_does_nothing_when_no_tunnels() -> None:
    """When no reverse tunnels are registered, _check_and_repair_tunnels is a no-op."""
    manager = _make_fake_reverse_tunnel_manager()
    manager._check_and_repair_tunnels()
    assert manager._setup_calls == []
    assert manager._write_calls == []
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
        agent_state_dirs=["~/.mngr/agents/agent-a", "~/.mngr/agents/agent-b"],
    )
    with manager._lock:
        manager._reverse_tunnels[conn_key] = tunnel_info

    manager._check_and_repair_tunnels()

    # setup_reverse_tunnel called for first dir, then again for the second dir
    assert len(manager._setup_calls) == 2
    assert manager._setup_calls[0][2] == "~/.mngr/agents/agent-a"
    assert manager._setup_calls[1][2] == "~/.mngr/agents/agent-b"
    # write_api_url_to_remote called for each agent state dir
    assert len(manager._write_calls) == 2
    for _, _agent_dir, url in manager._write_calls:
        assert url == "http://127.0.0.1:12345"
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
        agent_state_dirs=["~/.mngr/agents/agent-a"],
    )
    with manager._lock:
        manager._reverse_tunnels[conn_key] = tunnel_info

    manager._check_and_repair_tunnels()

    assert len(manager._setup_calls) == 1
    assert manager._write_calls == []
    manager.cleanup()


def test_check_and_repair_tunnels_empty_agent_dirs(tmp_path: Path) -> None:
    """When agent_state_dirs is empty, setup is called with an empty string."""
    manager = _make_fake_reverse_tunnel_manager(remote_port=7777)
    ssh_info = _sample_ssh_info(tmp_path)
    conn_key = "192.0.2.1:22"
    tunnel_info = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=5000,
        agent_state_dirs=[],
    )
    with manager._lock:
        manager._reverse_tunnels[conn_key] = tunnel_info

    manager._check_and_repair_tunnels()

    assert len(manager._setup_calls) == 1
    assert manager._setup_calls[0][2] == ""
    assert manager._write_calls == []
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
        agent_state_dirs=["~/.mngr/agents/agent-x"],
    )
    fake_client = FakeSSHClient.create(active=True)
    with manager._lock:
        manager._reverse_tunnels[conn_key] = tunnel_info
        manager._connections[conn_key] = fake_client

    manager._check_and_repair_tunnels()

    # No re-establishment attempted since the tunnel is alive
    assert manager._setup_calls == []
    assert manager._write_calls == []
    manager.cleanup()


# -- write_api_url_to_remote tests --
#
# These tests inject a FakeSSHClient directly into the manager's _connections
# dict (a private PrivateAttr) to avoid needing a real SSH server.
# This setup pattern matches the existing tests above that inject _reverse_tunnels.


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


def test_write_api_url_to_remote_succeeds(tmp_path: Path) -> None:
    """write_api_url_to_remote executes the correct shell command via SSH."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create()
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.write_api_url_to_remote(
        ssh_info=ssh_info,
        agent_state_dir="~/.mngr/agents/test-agent",
        url="http://127.0.0.1:8420",
    )

    assert len(fake_client._exec_calls) == 1
    assert "minds_api_url" in fake_client._exec_calls[0]
    assert "8420" in fake_client._exec_calls[0]
    manager.cleanup()


def test_write_api_url_to_remote_logs_on_nonzero_exit(tmp_path: Path) -> None:
    """write_api_url_to_remote logs a warning when the remote command exits non-zero."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(exec_exit_status=1)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.write_api_url_to_remote(
        ssh_info=ssh_info,
        agent_state_dir="/tmp/agent",
        url="http://127.0.0.1:9000",
    )

    assert len(fake_client._exec_calls) == 1
    manager.cleanup()


def test_write_api_url_to_remote_handles_ssh_exception(tmp_path: Path) -> None:
    """write_api_url_to_remote catches paramiko.SSHException without propagating."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(exec_raise=paramiko.SSHException)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.write_api_url_to_remote(
        ssh_info=ssh_info,
        agent_state_dir="/tmp/agent",
        url="http://127.0.0.1:9000",
    )
    manager.cleanup()


# -- setup_reverse_tunnel tests --
#
# These tests inject a FakeSSHClient directly into _connections so that
# setup_reverse_tunnel can run without making real SSH connections.


def test_setup_reverse_tunnel_returns_assigned_port(tmp_path: Path) -> None:
    """setup_reverse_tunnel calls request_port_forward and returns the assigned port."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    remote_port = manager.setup_reverse_tunnel(
        ssh_info=ssh_info,
        local_port=8420,
        agent_state_dir="~/.mngr/agents/test-agent",
    )

    assert remote_port == 54321
    manager.cleanup()


def test_setup_reverse_tunnel_stores_tunnel_info(tmp_path: Path) -> None:
    """After setup, the tunnel info is stored in _reverse_tunnels."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    manager.setup_reverse_tunnel(
        ssh_info=ssh_info,
        local_port=8420,
        agent_state_dir="~/.mngr/agents/test-agent",
    )

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    with manager._lock:
        tunnel_info = manager._reverse_tunnels.get(conn_key)

    assert tunnel_info is not None
    assert tunnel_info.remote_port == 54321
    assert tunnel_info.local_port == 8420
    assert "~/.mngr/agents/test-agent" in tunnel_info.agent_state_dirs
    manager.cleanup()


def test_setup_reverse_tunnel_reuses_existing_active_tunnel(tmp_path: Path) -> None:
    """When an active reverse tunnel already exists for a host, the same port is returned."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    existing_tunnel = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=11111,
        agent_state_dirs=["~/.mngr/agents/agent-a"],
    )
    with manager._lock:
        manager._reverse_tunnels[conn_key] = existing_tunnel

    port = manager.setup_reverse_tunnel(
        ssh_info=ssh_info,
        local_port=8420,
        agent_state_dir="~/.mngr/agents/agent-b",
    )

    assert port == 11111
    with manager._lock:
        tunnel_info = manager._reverse_tunnels[conn_key]
    assert "~/.mngr/agents/agent-b" in tunnel_info.agent_state_dirs
    manager.cleanup()


def test_setup_reverse_tunnel_does_not_duplicate_agent_dir(tmp_path: Path) -> None:
    """Calling setup with an agent_state_dir already tracked does not duplicate it."""
    ssh_info = _sample_ssh_info(tmp_path)
    fake_client = FakeSSHClient.create(active=True)
    manager = _make_manager_with_fake_connection(ssh_info, fake_client)

    conn_key = f"{ssh_info.host}:{ssh_info.port}"
    existing_tunnel = ReverseTunnelInfo(
        ssh_info=ssh_info,
        local_port=8420,
        remote_port=11111,
        agent_state_dirs=["~/.mngr/agents/agent-a"],
    )
    with manager._lock:
        manager._reverse_tunnels[conn_key] = existing_tunnel

    manager.setup_reverse_tunnel(
        ssh_info=ssh_info,
        local_port=8420,
        agent_state_dir="~/.mngr/agents/agent-a",
    )

    with manager._lock:
        tunnel_info = manager._reverse_tunnels[conn_key]
    assert tunnel_info.agent_state_dirs.count("~/.mngr/agents/agent-a") == 1
    manager.cleanup()
