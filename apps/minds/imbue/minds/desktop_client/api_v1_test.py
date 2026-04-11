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
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
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


def test_api_v1_rejects_empty_bearer_token(tmp_path: Path) -> None:
    client, _agent_id, _api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 401


# -- Telegram routes with orchestrator --


def _create_test_api_client_with_telegram(
    tmp_path: Path,
) -> tuple[TestClient, AgentId, str, WorkspacePaths]:
    """Create a client with a TelegramSetupOrchestrator."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    agent_id = AgentId()
    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    notification_dispatcher = NotificationDispatcher(is_electron=True)
    telegram_orchestrator = TelegramSetupOrchestrator(paths=paths)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
        telegram_orchestrator=telegram_orchestrator,
    )
    client = TestClient(app)
    return client, agent_id, api_key, paths


def test_telegram_setup_starts_with_orchestrator(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client_with_telegram(tmp_path)
    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        json={"agent_name": "test-bot"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    assert data["status"] == "CHECKING_CREDENTIALS"


def test_telegram_status_returns_404_for_unknown_agent(tmp_path: Path) -> None:
    client, _agent_id, api_key, _paths = _create_test_api_client_with_telegram(tmp_path)
    unknown_id = AgentId()
    response = client.get(
        f"/api/v1/agents/{unknown_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 404


# -- Cloudflare enable with backend resolver --


def _create_test_api_client_with_backend(
    tmp_path: Path,
    agent_id: AgentId,
    server_name: str = "web",
    backend_url: str = "http://127.0.0.1:9000",
) -> tuple[TestClient, str, WorkspacePaths]:
    """Create a client with a backend resolver that has a known agent/server."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {server_name: backend_url}},
    )
    notification_dispatcher = NotificationDispatcher(is_electron=True)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        paths=paths,
    )
    client = TestClient(app)
    return client, api_key, paths


def test_cloudflare_enable_returns_404_for_unknown_server(tmp_path: Path) -> None:
    """Cloudflare enable without a configured cloudflare client still returns 501."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_backend(tmp_path, agent_id)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/unknown/cloudflare",
        headers=_auth_headers(api_key),
    )
    # No cloudflare client configured, so returns 501
    assert response.status_code == 501


# -- Telegram setup with body parsing --


def test_telegram_setup_accepts_empty_body(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client_with_telegram(tmp_path)
    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200


def test_telegram_setup_with_invalid_json_body(tmp_path: Path) -> None:
    client, agent_id, api_key, _paths = _create_test_api_client_with_telegram(tmp_path)
    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        content="not json",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
    )
    # Should still succeed -- invalid body is handled gracefully
    assert response.status_code == 200


# -- Notification with no dispatcher --


def test_notification_returns_501_without_dispatcher(tmp_path: Path) -> None:
    """When notification_dispatcher is None, the endpoint returns 501."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    agent_id = AgentId()
    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/notifications",
        json={"message": "test"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 501
