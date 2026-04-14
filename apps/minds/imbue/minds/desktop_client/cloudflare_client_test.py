from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.desktop_client.cloudflare_client import CloudflareSecret
from imbue.minds.desktop_client.cloudflare_client import CloudflareUsername
from imbue.minds.desktop_client.cloudflare_client import OwnerEmail
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
    truncated_id = client._truncate_agent_id(agent_id)
    assert tunnel_name == f"testuser--{truncated_id}"


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


def test_auth_header_uses_bearer_when_supertokens_token_set() -> None:
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        supertokens_token="my-jwt-token",
        supertokens_user_id_prefix="a1b2c3d4e5f67890",
    )
    header = client._auth_header()
    assert header == "Bearer my-jwt-token"


def test_auth_header_prefers_supertokens_over_basic() -> None:
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        username=CloudflareUsername("testuser"),
        secret=CloudflareSecret("testsecret"),
        supertokens_token="my-jwt-token",
        supertokens_user_id_prefix="a1b2c3d4e5f67890",
    )
    header = client._auth_header()
    assert header.startswith("Bearer ")


def test_make_tunnel_name_uses_supertokens_user_id_prefix() -> None:
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        supertokens_token="token",
        supertokens_user_id_prefix="a1b2c3d4e5f67890",
    )
    agent_id = AgentId()
    tunnel_name = client.make_tunnel_name(agent_id)
    assert tunnel_name.startswith("a1b2c3d4e5f67890--")


def test_effective_owner_email_prefers_supertokens() -> None:
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        owner_email=OwnerEmail("basic@example.com"),
        supertokens_email="st@example.com",
    )
    assert client.effective_owner_email() == "st@example.com"


def test_effective_owner_email_falls_back_to_owner_email() -> None:
    client = _make_client()
    assert client.effective_owner_email() == "test@example.com"


def test_client_works_with_only_supertokens_fields() -> None:
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        supertokens_token="jwt-token",
        supertokens_user_id_prefix="a1b2c3d4e5f67890",
        supertokens_email="user@example.com",
    )
    assert client._auth_header() == "Bearer jwt-token"
    assert client._effective_username() == "a1b2c3d4e5f67890"
    assert client.effective_owner_email() == "user@example.com"
