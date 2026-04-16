from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import PrivateAttr

from imbue.minds.desktop_client.runner import AgentDiscoveryHandler
from imbue.minds.desktop_client.runner import _build_cloudflare_client
from imbue.minds.desktop_client.ssh_tunnel import RemoteSSHInfo
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelError
from imbue.minds.desktop_client.ssh_tunnel import SSHTunnelManager
from imbue.mngr.primitives import AgentId


def test_agent_discovery_handler_writes_local_url_file(tmp_path: Path) -> None:
    """Verify local agents get a minds_api_url file written."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=8420,
        mngr_host_dir=tmp_path,
        data_dir=tmp_path.parent,
    )

    agent_id = AgentId()

    # Call the handler with no SSH info (local agent)
    handler(agent_id, None, "local")

    url_file = tmp_path / "agents" / str(agent_id) / "minds_api_url"
    assert url_file.exists(), "minds_api_url file was not written"
    assert url_file.read_text() == "http://127.0.0.1:8420"

    tunnel_manager.cleanup()


def test_agent_discovery_handler_callable(tmp_path: Path) -> None:
    """Verify AgentDiscoveryHandler is callable with the expected signature."""
    tunnel_manager = SSHTunnelManager()
    handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=9000,
        mngr_host_dir=tmp_path,
        data_dir=tmp_path.parent,
    )
    assert callable(handler)
    assert handler.server_port == 9000
    tunnel_manager.cleanup()


def test_build_cloudflare_client_returns_none_without_basic_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without CLOUDFLARE_FORWARDING_USERNAME/SECRET, the raw client is None.

    The SuperTokens-enriched client is built from scratch in
    api_v1.get_cf_client_with_auth using minds_config.cloudflare_forwarding_url,
    so callers do not need a raw client just to hit Cloudflare via SuperTokens.
    """
    for key in (
        "CLOUDFLARE_FORWARDING_USERNAME",
        "CLOUDFLARE_FORWARDING_SECRET",
        "OWNER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    forwarding_url = AnyUrl("https://example.com/")
    result = _build_cloudflare_client(forwarding_url)
    assert result is None


def test_build_cloudflare_client_returns_none_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Username set but secret unset: Basic Auth is still unusable, so no raw client."""
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_USERNAME", "user")
    monkeypatch.delenv("CLOUDFLARE_FORWARDING_SECRET", raising=False)
    monkeypatch.delenv("OWNER_EMAIL", raising=False)
    forwarding_url = AnyUrl("https://example.com/")
    result = _build_cloudflare_client(forwarding_url)
    assert result is None


def test_build_cloudflare_client_reads_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auth fields come from env vars; URL comes from the parameter."""
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_USERNAME", "user")
    monkeypatch.setenv("CLOUDFLARE_FORWARDING_SECRET", "secret")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    forwarding_url = AnyUrl("https://example.com/")
    result = _build_cloudflare_client(forwarding_url)
    assert result is not None
    assert str(result.forwarding_url) == "https://example.com/"
    assert str(result.username) == "user"


def test_agent_discovery_handler_handles_local_write_error(tmp_path: Path) -> None:
    """Verify local agent write errors are logged but do not propagate.

    Placing a file at the agents/ path prevents mkdir from creating the
    subdirectory, causing an OSError that should be caught and logged.
    """
    tunnel_manager = SSHTunnelManager()
    blocker = tmp_path / "agents"
    blocker.write_text("block")
    handler = AgentDiscoveryHandler(
        tunnel_manager=tunnel_manager,
        server_port=8420,
        mngr_host_dir=tmp_path,
        data_dir=tmp_path.parent,
    )
    agent_id = AgentId()
    handler(agent_id, None, "local")
    tunnel_manager.cleanup()


class _FakeTunnelManager(SSHTunnelManager):
    """Test double for SSHTunnelManager that records calls instead of making SSH connections."""

    _fake_remote_port: int = PrivateAttr(default=55000)
    _fake_fail: bool = PrivateAttr(default=False)
    _reverse_tunnel_calls: list[tuple[RemoteSSHInfo, int, str]] = PrivateAttr(default_factory=list)
    _write_remote_calls: list[tuple[RemoteSSHInfo, str, str]] = PrivateAttr(default_factory=list)

    @classmethod
    def create(cls, remote_port: int = 55000, fail: bool = False) -> "_FakeTunnelManager":
        mgr = cls()
        mgr._fake_remote_port = remote_port
        mgr._fake_fail = fail
        return mgr

    def setup_reverse_tunnel(
        self,
        ssh_info: RemoteSSHInfo,
        local_port: int,
        agent_state_dir: str,
    ) -> int:
        self._reverse_tunnel_calls.append((ssh_info, local_port, agent_state_dir))
        if self._fake_fail:
            raise SSHTunnelError("simulated failure")
        return self._fake_remote_port

    def write_api_url_to_remote(
        self,
        ssh_info: RemoteSSHInfo,
        agent_state_dir: str,
        url: str,
    ) -> None:
        self._write_remote_calls.append((ssh_info, agent_state_dir, url))


def test_agent_discovery_handler_handles_remote_agent(tmp_path: Path) -> None:
    """Verify remote agents get a reverse tunnel set up and URL written to remote."""
    fake_manager = _FakeTunnelManager.create(remote_port=12345)
    ssh_info = RemoteSSHInfo(
        user="root",
        host="192.168.1.100",
        port=22,
        key_path=tmp_path / "key",
    )
    handler = AgentDiscoveryHandler(
        tunnel_manager=fake_manager,
        server_port=8420,
        mngr_host_dir=tmp_path,
        data_dir=tmp_path,
    )
    agent_id = AgentId()
    handler(agent_id, ssh_info, "docker")

    assert len(fake_manager._reverse_tunnel_calls) == 1
    _, local_port, agent_state_dir = fake_manager._reverse_tunnel_calls[0]
    assert local_port == 8420
    assert agent_state_dir == f"/mngr/agents/{agent_id}"

    assert len(fake_manager._write_remote_calls) == 1
    _, _, url = fake_manager._write_remote_calls[0]
    assert url == "http://127.0.0.1:12345"
    fake_manager.cleanup()


def test_agent_discovery_handler_handles_remote_agent_tunnel_error(tmp_path: Path) -> None:
    """Verify SSHTunnelError during remote setup is caught and logged, not propagated."""
    fake_manager = _FakeTunnelManager.create(fail=True)
    ssh_info = RemoteSSHInfo(
        user="root",
        host="192.168.1.100",
        port=22,
        key_path=tmp_path / "key",
    )
    handler = AgentDiscoveryHandler(
        tunnel_manager=fake_manager,
        server_port=8420,
        mngr_host_dir=tmp_path,
        data_dir=tmp_path,
    )
    agent_id = AgentId()
    # Should not raise even though setup_reverse_tunnel raises SSHTunnelError
    handler(agent_id, ssh_info, "docker")
    assert len(fake_manager._write_remote_calls) == 0
    fake_manager.cleanup()
