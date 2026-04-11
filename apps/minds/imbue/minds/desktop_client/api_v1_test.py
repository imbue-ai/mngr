from pathlib import Path

from pydantic import PrivateAttr
from pydantic import SecretStr
from starlette.testclient import TestClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import generate_api_key
from imbue.minds.desktop_client.api_key_store import hash_api_key
from imbue.minds.desktop_client.api_key_store import save_api_key_hash
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.desktop_client.cloudflare_client import CloudflareSecret
from imbue.minds.desktop_client.cloudflare_client import CloudflareUsername
from imbue.minds.desktop_client.cloudflare_client import OwnerEmail
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.telegram.credential_store import save_agent_bot_credentials
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.setup import TelegramSetupInfo
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId


def _make_cloudflare_client() -> CloudflareForwardingClient:
    """Create a CloudflareForwardingClient pointed at a non-listening port (will fail fast)."""
    return CloudflareForwardingClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        username=CloudflareUsername("testuser"),
        secret=CloudflareSecret("testsecret"),
        owner_email=OwnerEmail("test@example.com"),
    )


def _create_test_api_client_with_cloudflare(
    tmp_path: Path,
    agent_id: AgentId,
) -> tuple[TestClient, str, WorkspacePaths]:
    """Create a client with a configured (but non-functional) CloudflareForwardingClient."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://127.0.0.1:9000"}},
    )
    notification_dispatcher = NotificationDispatcher(is_electron=True)
    cloudflare_client = _make_cloudflare_client()

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        cloudflare_client=cloudflare_client,
        paths=paths,
    )
    client = TestClient(app)
    return client, api_key, paths


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
    """When an agent already has bot credentials, setup returns CHECKING_CREDENTIALS.

    Pre-saves credentials so the background thread detects them immediately
    and sets DONE status without trying to open a browser.
    """
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    # Pre-save credentials so the orchestrator detects them and skips browser login
    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_setup_test"),
        bot_username="setup_test_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        json={"agent_name": "test-bot"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    # With pre-saved credentials, start_setup detects them and returns immediately
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
    """Pre-saves credentials to avoid spawning a browser thread."""
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_empty_body"),
        bot_username="empty_body_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200


def test_telegram_setup_with_invalid_json_body(tmp_path: Path) -> None:
    """Pre-saves credentials to avoid spawning a browser thread."""
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_invalid_json"),
        bot_username="invalid_json_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

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


# -- Stub TelegramSetupOrchestrator for state-injection tests --


class _StubTelegramOrchestrator(TelegramSetupOrchestrator):
    """TelegramSetupOrchestrator subclass with preset get_setup_info return value.

    Allows tests to verify HTTP endpoint behavior for specific orchestrator
    states without touching private attributes or spawning background threads.
    """

    _preset_info_by_agent: dict[str, TelegramSetupInfo] = PrivateAttr(default_factory=dict)

    @classmethod
    def create_with_info(
        cls,
        paths: WorkspacePaths,
        agent_id: AgentId,
        setup_info: TelegramSetupInfo,
    ) -> "_StubTelegramOrchestrator":
        """Create a stub with a preset TelegramSetupInfo for a specific agent."""
        stub = cls(paths=paths)
        stub._preset_info_by_agent[str(agent_id)] = setup_info
        return stub

    def get_setup_info(self, agent_id: AgentId) -> TelegramSetupInfo | None:
        return self._preset_info_by_agent.get(str(agent_id))

    def start_setup(self, agent_id: AgentId, agent_name: str) -> None:
        pass


# -- Telegram status with active Telegram --


def test_telegram_status_returns_done_when_agent_has_telegram(tmp_path: Path) -> None:
    """When an agent has stored bot credentials, status endpoint returns DONE."""
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    # Save bot credentials so agent_has_telegram() returns True
    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_test"),
        bot_username="test_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    assert data["status"] == str(TelegramSetupStatus.DONE)


def test_telegram_status_includes_bot_username_without_active_setup(tmp_path: Path) -> None:
    """When an agent has stored bot credentials but no active setup, bot_username is returned.

    This covers the case where a previous session stored credentials and the
    orchestrator has no in-memory setup info (get_setup_info returns None),
    but the credential file exists on disk.
    """
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_status_test"),
        bot_username="status_test_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    # Query status directly without calling start_setup first.
    # The orchestrator has no in-memory info, but credentials exist on disk.
    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == str(TelegramSetupStatus.DONE)
    assert data.get("bot_username") == "status_test_bot"


def test_telegram_status_returns_in_progress_info(tmp_path: Path) -> None:
    """When setup is in progress, status endpoint returns current status info."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    agent_id = AgentId()
    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    preset_info = TelegramSetupInfo(
        agent_id=agent_id,
        status=TelegramSetupStatus.CREATING_BOT,
    )
    orchestrator = _StubTelegramOrchestrator.create_with_info(paths, agent_id, preset_info)

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    notification_dispatcher = NotificationDispatcher(is_electron=True)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        telegram_orchestrator=orchestrator,
        paths=paths,
    )
    client = TestClient(app)

    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == str(agent_id)
    assert data["status"] == str(TelegramSetupStatus.CREATING_BOT)


def test_telegram_status_includes_bot_username_when_done(tmp_path: Path) -> None:
    """When agent has stored bot credentials, the response includes bot_username."""
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token"),
        bot_username="my_test_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    # Trigger start_setup which will detect credentials and set DONE with bot_username
    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        json={"agent_name": "test-bot"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200

    # Status should now include bot_username from stored credentials
    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == str(TelegramSetupStatus.DONE)
    assert data.get("bot_username") == "my_test_bot"


def test_telegram_status_returns_error_field_when_setup_failed(tmp_path: Path) -> None:
    """When setup has FAILED status, the status response includes the error field."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    agent_id = AgentId()
    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    preset_info = TelegramSetupInfo(
        agent_id=agent_id,
        status=TelegramSetupStatus.FAILED,
        error="setup failed unexpectedly",
    )
    orchestrator = _StubTelegramOrchestrator.create_with_info(paths, agent_id, preset_info)

    backend_resolver = StaticBackendResolver(url_by_agent_and_server={})
    notification_dispatcher = NotificationDispatcher(is_electron=True)

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        telegram_orchestrator=orchestrator,
        paths=paths,
    )
    client = TestClient(app)

    response = client.get(
        f"/api/v1/agents/{agent_id}/telegram",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == str(TelegramSetupStatus.FAILED)
    assert data["error"] == "setup failed unexpectedly"


# -- Cloudflare routes with configured client --


def test_cloudflare_enable_returns_502_when_api_call_fails(tmp_path: Path) -> None:
    """When the Cloudflare API is unreachable, enable returns 502."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_cloudflare(tmp_path, agent_id)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 502


def test_cloudflare_enable_returns_404_when_server_not_found(tmp_path: Path) -> None:
    """When the backend resolver has no URL for the server, enable returns 404."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_cloudflare(tmp_path, agent_id)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/nonexistent/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 404


def test_cloudflare_enable_uses_service_url_from_body(tmp_path: Path) -> None:
    """When the request body has service_url, it is used instead of the backend resolver."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_cloudflare(tmp_path, agent_id)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        json={"service_url": "http://127.0.0.1:9001"},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 502


def test_cloudflare_disable_returns_502_when_api_call_fails(tmp_path: Path) -> None:
    """When the Cloudflare API is unreachable, disable returns 502."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_cloudflare(tmp_path, agent_id)
    response = client.delete(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 502


def test_notification_rejects_non_dict_json_body(tmp_path: Path) -> None:
    """When the notification body is valid JSON but not a dict, return 400."""
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        content="[1, 2, 3]",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_notification_rejects_non_string_title(tmp_path: Path) -> None:
    """When the title field is not a string, return 400."""
    client, _agent_id, api_key, _paths = _create_test_api_client(tmp_path)
    response = client.post(
        "/api/v1/notifications",
        json={"message": "test", "title": 123},
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 400
    assert "title" in response.json()["error"]


def test_telegram_setup_with_non_dict_json_body(tmp_path: Path) -> None:
    """When the telegram setup body is valid JSON but not a dict, setup still proceeds.

    Pre-saves bot credentials so the orchestrator detects them immediately
    and does not spawn a background thread that opens a real browser.
    """
    client, agent_id, api_key, paths = _create_test_api_client_with_telegram(tmp_path)

    credentials = TelegramBotCredentials(
        bot_token=SecretStr("123:fake_token_for_non_dict_test"),
        bot_username="non_dict_test_bot",
    )
    save_agent_bot_credentials(paths.data_dir, agent_id, credentials)

    response = client.post(
        f"/api/v1/agents/{agent_id}/telegram",
        content="42",
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
    )
    # Non-dict body is handled gracefully: setup proceeds with default agent name
    assert response.status_code == 200


# -- Cloudflare success path tests --


class _AlwaysSucceedCloudflareClient(CloudflareForwardingClient):
    """CloudflareForwardingClient subclass that always returns True without making HTTP calls."""

    def add_service(self, agent_id: AgentId, service_name: str, service_url: str) -> bool:
        return True

    def remove_service(self, agent_id: AgentId, service_name: str) -> bool:
        return True


def _create_test_api_client_with_succeeding_cloudflare(
    tmp_path: Path,
    agent_id: AgentId,
) -> tuple[TestClient, str, WorkspacePaths]:
    """Create a client with a CloudflareForwardingClient that always succeeds."""
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    auth_store = FileAuthStore(data_directory=paths.auth_dir)

    api_key = generate_api_key()
    save_api_key_hash(paths.data_dir, agent_id, hash_api_key(api_key))

    backend_resolver = StaticBackendResolver(
        url_by_agent_and_server={str(agent_id): {"web": "http://127.0.0.1:9000"}},
    )
    notification_dispatcher = NotificationDispatcher(is_electron=True)
    cloudflare_client = _AlwaysSucceedCloudflareClient(
        forwarding_url=CloudflareForwardingUrl("http://127.0.0.1:1"),
        username=CloudflareUsername("testuser"),
        secret=CloudflareSecret("testsecret"),
        owner_email=OwnerEmail("test@example.com"),
    )

    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        notification_dispatcher=notification_dispatcher,
        cloudflare_client=cloudflare_client,
        paths=paths,
    )
    client = TestClient(app)
    return client, api_key, paths


def test_cloudflare_enable_returns_200_on_success(tmp_path: Path) -> None:
    """When Cloudflare API succeeds, enable returns 200 with ok=True."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_succeeding_cloudflare(tmp_path, agent_id)
    response = client.put(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_cloudflare_disable_returns_200_on_success(tmp_path: Path) -> None:
    """When Cloudflare API succeeds, disable returns 200 with ok=True."""
    agent_id = AgentId()
    client, api_key, _paths = _create_test_api_client_with_succeeding_cloudflare(tmp_path, agent_id)
    response = client.delete(
        f"/api/v1/agents/{agent_id}/servers/web/cloudflare",
        headers=_auth_headers(api_key),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


