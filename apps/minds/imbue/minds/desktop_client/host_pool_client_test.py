from uuid import UUID

import pytest

from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.host_pool_client import HostPoolEmptyError
from imbue.minds.desktop_client.host_pool_client import HostPoolError
from imbue.minds.desktop_client.host_pool_client import LeaseHostResult
from imbue.minds.errors import MindError


def _make_client(url: str = "http://127.0.0.1:1") -> HostPoolClient:
    return HostPoolClient(
        connector_url=RemoteServiceConnectorUrl(url),
    )


def test_url_construction_strips_trailing_slash() -> None:
    client = _make_client("http://example.com/")
    assert client._url("/hosts/lease") == "http://example.com/hosts/lease"


def test_url_construction_without_trailing_slash() -> None:
    client = _make_client("http://example.com")
    assert client._url("/hosts/lease") == "http://example.com/hosts/lease"


def test_host_pool_error_inherits_from_mind_error() -> None:
    """HostPoolError should be catchable as a MindError."""
    error = HostPoolError("test")
    assert isinstance(error, MindError)


def test_host_pool_empty_error_inherits_from_host_pool_error() -> None:
    """HostPoolEmptyError should be catchable as a HostPoolError."""
    error = HostPoolEmptyError("no hosts")
    assert isinstance(error, HostPoolError)
    assert isinstance(error, MindError)


def test_lease_host_raises_on_connection_error() -> None:
    """Leasing from an unreachable server raises HostPoolError."""
    client = _make_client()
    with pytest.raises(HostPoolError, match="lease request failed"):
        client.lease_host(access_token="token", ssh_public_key="ssh-ed25519 AAAA", version="v1")


def test_release_host_returns_false_on_connection_error() -> None:
    """Releasing to an unreachable server returns False without raising."""
    client = _make_client()
    result = client.release_host(access_token="token", host_db_id=UUID("00000000-0000-0000-0000-000000000042"))
    assert result is False


def test_list_leased_hosts_returns_empty_on_connection_error() -> None:
    """Listing from an unreachable server returns an empty list without raising."""
    client = _make_client()
    result = client.list_leased_hosts(access_token="token")
    assert result == []


# -- Happy path tests with fake server --


@pytest.mark.flaky
def test_lease_host_happy_path(fake_pool_server: HostPoolClient) -> None:
    result = fake_pool_server.lease_host(
        access_token="test-token",
        ssh_public_key="ssh-ed25519 AAAA test",
        version="v0.1.0",
    )
    assert isinstance(result, LeaseHostResult)
    assert result.host_db_id == UUID("a1b2c3d4-e5f6-7890-1234-567890abcdef")
    assert result.vps_ip == "203.0.113.10"
    assert result.container_ssh_port == 2222
    assert result.agent_id == "agent-abc12300000000000000000000000000"
    assert result.version == "v0.1.0"


def test_release_host_happy_path(fake_pool_server: HostPoolClient) -> None:
    result = fake_pool_server.release_host(
        access_token="test-token", host_db_id=UUID("a1b2c3d4-e5f6-7890-1234-567890abcdef")
    )
    assert result is True


def test_list_leased_hosts_happy_path(fake_pool_server: HostPoolClient) -> None:
    hosts = fake_pool_server.list_leased_hosts(access_token="test-token")
    assert len(hosts) == 1
    assert hosts[0].host_db_id == UUID("a1b2c3d4-e5f6-7890-1234-567890abcdef")
    assert hosts[0].vps_ip == "203.0.113.10"
    assert hosts[0].leased_at == "2026-01-01T00:00:00Z"
