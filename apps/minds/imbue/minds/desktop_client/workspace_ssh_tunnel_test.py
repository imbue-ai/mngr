from pathlib import Path

import pytest

from imbue.minds.desktop_client.workspace_ssh_tunnel import WorkspaceSshTunnelError
from imbue.minds.desktop_client.workspace_ssh_tunnel import broker_reverse_tunnel_into_caller
from imbue.minds.desktop_client.workspace_ssh_tunnel import is_loopback_host
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelError
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager


class _StubTunnelManager(SSHTunnelManager):
    """A tunnel manager that records the reverse-tunnel request instead of doing SSH."""

    forced_port: int = 0
    should_raise: bool = False
    recorded_local_port: int = -1
    recorded_caller_host: str = ""
    recorded_agent_id: str = ""

    def setup_reverse_tunnel(
        self, ssh_info: RemoteSSHInfo, local_port: int, remote_port: int = 0, agent_id: str | None = None
    ) -> int:
        if self.should_raise:
            raise SSHTunnelError("connect refused")
        self.recorded_local_port = local_port
        self.recorded_caller_host = ssh_info.host
        self.recorded_agent_id = agent_id or ""
        return self.forced_port


def _ssh(host: str, port: int) -> RemoteSSHInfo:
    return RemoteSSHInfo(user="root", host=host, port=port, key_path=Path("/keys/k"))


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "LOCALHOST", "  127.0.0.1  "])
def test_is_loopback_host_true_for_loopback_addresses(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["remote.example.com", "10.0.0.5", "1.2.3.4", "modal-x.modal.host", ""])
def test_is_loopback_host_false_for_routable_hosts(host: str) -> None:
    assert is_loopback_host(host) is False


def test_broker_reverse_tunnel_forwards_caller_listener_to_target_port() -> None:
    manager = _StubTunnelManager(forced_port=43210)
    caller = _ssh("203.0.113.7", 22)
    target = _ssh("127.0.0.1", 49222)

    assigned_port = broker_reverse_tunnel_into_caller(
        manager, caller_ssh=caller, target_ssh=target, target_agent_id="agent-target"
    )

    # The caller gets the assigned loopback port, and the hub relays it to the
    # target's published sshd port via a tunnel into the caller's host.
    assert assigned_port == 43210
    assert manager.recorded_local_port == 49222
    assert manager.recorded_caller_host == "203.0.113.7"
    assert manager.recorded_agent_id == "agent-target"


def test_broker_reverse_tunnel_wraps_manager_errors() -> None:
    manager = _StubTunnelManager(should_raise=True)
    caller = _ssh("203.0.113.7", 22)
    target = _ssh("127.0.0.1", 49222)

    with pytest.raises(WorkspaceSshTunnelError):
        broker_reverse_tunnel_into_caller(
            manager, caller_ssh=caller, target_ssh=target, target_agent_id="agent-target"
        )
