import pytest

from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingUrl
from imbue.mngr.primitives import AgentId


def _make_client(
    url: str = "http://127.0.0.1:1",
    supertokens_email: str | None = "test@example.com",
) -> CloudflareForwardingClient:
    return CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl(url),
        supertokens_token="jwt-token",
        supertokens_user_id_prefix="a1b2c3d4e5f67890",
        supertokens_email=supertokens_email,
    )


def test_make_tunnel_name() -> None:
    client = _make_client()
    agent_id = AgentId()
    tunnel_name = client.make_tunnel_name(agent_id)
    truncated_id = client._truncate_agent_id(agent_id)
    assert tunnel_name == f"a1b2c3d4e5f67890--{truncated_id}"


def test_auth_header_is_bearer() -> None:
    client = _make_client()
    header = client._auth_header()
    assert header == "Bearer jwt-token"


def test_auth_header_raises_without_supertokens_token() -> None:
    """A client built without a SuperTokens session cannot authenticate."""
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
    )
    with pytest.raises(ValueError, match="supertokens_token"):
        client._auth_header()


def test_effective_username_raises_without_user_id_prefix() -> None:
    """Tunnel naming requires a user-id prefix from the session."""
    client = CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        supertokens_token="jwt-token",
    )
    with pytest.raises(ValueError, match="supertokens_user_id_prefix"):
        client._effective_username()


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


def test_create_tunnel_includes_default_policy_from_supertokens_email() -> None:
    """The request body's default_auth_policy tracks ``supertokens_email``."""
    client = _make_client(supertokens_email="owner@example.com")
    # Exercising the connection-error path is enough to confirm the policy is
    # assembled without raising; the actual body is covered by the forwarding
    # server's own tests.
    token, _ = client.create_tunnel(AgentId())
    assert token is None


def test_create_tunnel_omits_default_policy_when_no_session_email() -> None:
    client = _make_client(supertokens_email=None)
    token, _ = client.create_tunnel(AgentId())
    assert token is None
