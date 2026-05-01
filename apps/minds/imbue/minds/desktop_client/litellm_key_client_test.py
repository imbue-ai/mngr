import pytest

from imbue.minds.desktop_client.cloudflare_client import RemoteServiceConnectorUrl
from imbue.minds.desktop_client.litellm_key_client import CreateKeyResult
from imbue.minds.desktop_client.litellm_key_client import KeyInfo
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyClient
from imbue.minds.desktop_client.litellm_key_client import LiteLLMKeyError
from imbue.minds.errors import MindError


def _make_client(url: str = "http://127.0.0.1:1") -> LiteLLMKeyClient:
    return LiteLLMKeyClient(
        connector_url=RemoteServiceConnectorUrl(url),
    )


def test_url_construction_strips_trailing_slash() -> None:
    client = _make_client("http://example.com/")
    assert client._url("/keys/create") == "http://example.com/keys/create"


def test_url_construction_without_trailing_slash() -> None:
    client = _make_client("http://example.com")
    assert client._url("/keys/create") == "http://example.com/keys/create"


def test_litellm_key_error_inherits_from_mind_error() -> None:
    error = LiteLLMKeyError("test")
    assert isinstance(error, MindError)


def test_create_key_raises_on_connection_error() -> None:
    client = _make_client()
    with pytest.raises(LiteLLMKeyError, match="creation request failed"):
        client.create_key(
            access_token="token",
            key_alias="test",
            max_budget=100.0,
            budget_duration="1d",
            metadata=None,
        )


def test_get_key_info_raises_on_connection_error() -> None:
    client = _make_client()
    with pytest.raises(LiteLLMKeyError, match="info request failed"):
        client.get_key_info(access_token="token", key_id="key-123")


def test_update_budget_raises_on_connection_error() -> None:
    client = _make_client()
    with pytest.raises(LiteLLMKeyError, match="update request failed"):
        client.update_budget(
            access_token="token",
            key_id="key-123",
            max_budget=50.0,
            budget_duration="1d",
        )


def test_delete_key_raises_on_connection_error() -> None:
    client = _make_client()
    with pytest.raises(LiteLLMKeyError, match="deletion request failed"):
        client.delete_key(access_token="token", key_id="key-123")


def test_list_keys_returns_empty_on_connection_error() -> None:
    client = _make_client()
    result = client.list_keys(access_token="token")
    assert result == []


def test_create_key_happy_path(fake_key_server: LiteLLMKeyClient) -> None:
    result = fake_key_server.create_key(
        access_token="test-token",
        key_alias="agent-test",
        max_budget=100.0,
        budget_duration="1d",
        metadata={"agent_id": "agent-abc123", "host_id": "host-def456"},
    )
    assert isinstance(result, CreateKeyResult)
    assert result.key == "sk-litellm-test-virtual-key-0123456789abcdef"
    assert result.base_url == "https://litellm-proxy.modal.run/anthropic"


def test_create_key_without_optional_params(fake_key_server: LiteLLMKeyClient) -> None:
    result = fake_key_server.create_key(
        access_token="test-token",
        key_alias=None,
        max_budget=None,
        budget_duration=None,
        metadata=None,
    )
    assert isinstance(result, CreateKeyResult)
    assert result.key == "sk-litellm-test-virtual-key-0123456789abcdef"


def test_list_keys_happy_path(fake_key_server: LiteLLMKeyClient) -> None:
    result = fake_key_server.list_keys(access_token="test-token")
    assert len(result) == 1
    assert isinstance(result[0], KeyInfo)
    assert result[0].token == "hashed-token-abc123"
    assert result[0].key_alias == "agent-test"
    assert result[0].spend == 12.50
    assert result[0].max_budget == 100.0
    assert result[0].budget_duration == "1d"


def test_get_key_info_happy_path(fake_key_server: LiteLLMKeyClient) -> None:
    result = fake_key_server.get_key_info(access_token="test-token", key_id="key-123")
    assert isinstance(result, KeyInfo)
    assert result.token == "hashed-token-abc123"
    assert result.spend == 12.50


def test_update_budget_happy_path(fake_key_server: LiteLLMKeyClient) -> None:
    fake_key_server.update_budget(
        access_token="test-token",
        key_id="key-123",
        max_budget=200.0,
        budget_duration="1w",
    )


def test_delete_key_happy_path(fake_key_server: LiteLLMKeyClient) -> None:
    fake_key_server.delete_key(access_token="test-token", key_id="key-123")
