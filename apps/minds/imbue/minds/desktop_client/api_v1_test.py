import json
from pathlib import Path

from starlette.testclient import TestClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.mngr.primitives import AgentId


def _create_test_api_client(
    tmp_path: Path,
    agent_id: AgentId | None = None,
    api_key: str | None = None,
) -> tuple[TestClient, AgentId, str, WorkspacePaths]:
    """Create a desktop client with the API v1 router and a valid API key."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    resolved_agent_id = agent_id or AgentId()
    resolved_api_key = api_key or generate_api_key()
    key_hash = hash_api_key(resolved_api_key)
    save_api_key_hash(paths.data_dir, resolved_agent_id, key_hash)

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    # Use Electron mode in tests to avoid tkinter side effects
    notification_dispatcher = NotificationDispatcher(is_electron=True)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
    )
    client = TestClient(app)
    return client, resolved_agent_id, resolved_api_key, paths


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# -- Auth tests --


def test_api_v1_rejects_missing_auth(tmp_path: Path) -> None:
    client, _agent_id, _api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post("/api/v1/notifications", json={"message": "test"})
    assert response.status_code == 401


def test_api_v1_rejects_invalid_bearer_token(tmp_path: Path) -> None:
    client, _agent_id, _api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers={"Authorization": "Bearer invalid-key"},
    )
    assert response.status_code == 401


def test_api_v1_rejects_malformed_auth_header(tmp_path: Path) -> None:
    client, _agent_id, _api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


def test_api_v1_accepts_valid_bearer_token(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers=_auth_headers(api_key),
    )
    # Should succeed (or 501 if dispatcher not configured, but we configured it)
    assert response.status_code == 200


# -- Cloudflare routes --


def test_cloudflare_enable_returns_501_when_not_configured(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 501
    assert "not configured" in response.json()["error"]


def test_cloudflare_disable_returns_501_when_not_configured(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.delete(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 501
    assert "not configured" in response.json()["error"]


# -- Telegram routes --


def test_telegram_setup_returns_501_when_not_configured(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 501
    assert "not configured" in response.json()["error"]


def test_telegram_status_returns_501_when_not_configured(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 501
    assert "not configured" in response.json()["error"]


# -- Notification route --


def test_notification_succeeds_with_valid_body(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "Hello user", "title": "Test", "urgency": "low"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_notification_rejects_missing_message(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"title": "No message field"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "message" in response.json()["error"]


def test_notification_rejects_invalid_urgency(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test", "urgency": "SUPER_URGENT"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "urgency" in response.json()["error"].lower()


def test_notification_rejects_invalid_json(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        content="not json",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_notification_defaults_urgency_to_normal(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
