from imbue.minds.forwarding_server.cloudflare_client import CloudflareForwardingClient
from imbue.minds.forwarding_server.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.forwarding_server.cloudflare_client import CloudflareSecret
from imbue.minds.forwarding_server.cloudflare_client import CloudflareUsername
from imbue.minds.forwarding_server.cloudflare_client import OwnerEmail
from imbue.mngr.primitives import AgentId


def _make_client(url: str = "http://127.0.0.1:1") -> CloudflareForwardingClient:
    return CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl(url),
        username=CloudflareUsername("testuser"),
        secret=CloudflareSecret("testsecret"),
        owner_email=OwnerEmail("test@example.com"),
    )


def test_make_tunnel_name() -> None:
    client = _make_client()
    agent_id = AgentId()
    tunnel_name = client.make_tunnel_name(agent_id)
    assert tunnel_name == f"testuser--{agent_id}"


def test_auth_header_is_basic() -> None:
    client = _make_client()
    header = client._auth_header()
    assert header.startswith("Basic ")


def test_create_tunnel_returns_none_and_message_on_connection_error() -> None:
    client = _make_client()
    token, message = client.create_tunnel(AgentId())
    assert token is None
    assert "failed" in message.lower()


def test_list_services_returns_none_on_connection_error() -> None:
    client = _make_client()
    result = client.list_services(AgentId())
    assert result is None


def test_add_service_returns_false_on_connection_error() -> None:
    client = _make_client()
    result = client.add_service(AgentId(), "web", "http://localhost:8080")
    assert result is False


def test_remove_service_returns_false_on_connection_error() -> None:
    client = _make_client()
    result = client.remove_service(AgentId(), "web")
    assert result is False
