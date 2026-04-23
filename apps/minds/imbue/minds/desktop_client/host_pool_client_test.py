import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Final

import pytest

from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.host_pool_client import HostPoolClient
from imbue.minds.desktop_client.host_pool_client import HostPoolEmptyError
from imbue.minds.desktop_client.host_pool_client import HostPoolError
from imbue.minds.desktop_client.host_pool_client import LeaseHostResult
from imbue.minds.errors import MindError

_FAKE_LEASE_RESPONSE: Final[dict[str, object]] = {
    "host_db_id": 7,
    "vps_ip": "203.0.113.10",
    "ssh_port": 22,
    "ssh_user": "root",
    "container_ssh_port": 2222,
    "agent_id": "agent-abc123",
    "host_id": "host-def456",
    "version": "v0.1.0",
}


class _FakePoolHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that returns canned responses for pool endpoints."""

    def do_POST(self) -> None:
        if self.path == "/hosts/lease":
            self._respond(200, _FAKE_LEASE_RESPONSE)
        elif self.path.endswith("/release"):
            self._respond(200, {"status": "released"})
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self) -> None:
        if self.path == "/hosts":
            self._respond(200, [dict(_FAKE_LEASE_RESPONSE, leased_at="2026-01-01T00:00:00Z")])
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: object) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture()
def fake_pool_server() -> Iterator[HostPoolClient]:
    """Start a local HTTP server and return a HostPoolClient pointing to it."""
    server = HTTPServer(("127.0.0.1", 0), _FakePoolHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = HostPoolClient(
        connector_url=RemoteServiceConnectorUrl("http://127.0.0.1:{}".format(port)),
    )
    yield client
    server.shutdown()


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
    result = client.release_host(access_token="token", host_db_id=42)
    assert result is False


def test_list_leased_hosts_returns_empty_on_connection_error() -> None:
    """Listing from an unreachable server returns an empty list without raising."""
    client = _make_client()
    result = client.list_leased_hosts(access_token="token")
    assert result == []


# -- Happy path tests with fake server --


def test_lease_host_happy_path(fake_pool_server: HostPoolClient) -> None:
    result = fake_pool_server.lease_host(
        access_token="test-token",
        ssh_public_key="ssh-ed25519 AAAA test",
        version="v0.1.0",
    )
    assert isinstance(result, LeaseHostResult)
    assert result.host_db_id == 7
    assert result.vps_ip == "203.0.113.10"
    assert result.container_ssh_port == 2222
    assert result.agent_id == "agent-abc123"
    assert result.version == "v0.1.0"


def test_release_host_happy_path(fake_pool_server: HostPoolClient) -> None:
    result = fake_pool_server.release_host(access_token="test-token", host_db_id=7)
    assert result is True


def test_list_leased_hosts_happy_path(fake_pool_server: HostPoolClient) -> None:
    hosts = fake_pool_server.list_leased_hosts(access_token="test-token")
    assert len(hosts) == 1
    assert hosts[0].host_db_id == 7
    assert hosts[0].vps_ip == "203.0.113.10"
    assert hosts[0].leased_at == "2026-01-01T00:00:00Z"
