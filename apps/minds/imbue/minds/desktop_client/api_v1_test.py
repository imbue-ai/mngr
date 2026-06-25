import json
from pathlib import Path

from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId
from imbue.mngr.primitives import AgentId

_TEST_KEY = "test-minds-api-key"


def _client_with_workspace(tmp_path: Path, agent_id: AgentId) -> FlaskClient:
    """Build a desktop-client test client with the /api/v1 surface mounted.

    Passing ``paths`` mounts the ``/api/v1`` blueprint, and ``minds_api_key``
    sets the bearer the routes require. The StaticBackendResolver reports the
    one workspace under both the known-agents and known-workspaces lists.
    """
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    return app.test_client()


def _auth_header() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TEST_KEY}"}


def _client_with_agent_creator(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> FlaskClient:
    """Build a test client whose ``/api/v1`` create route has an ``AgentCreator`` wired.

    The create route returns 501 when no ``AgentCreator`` is configured (before
    any input validation runs), so reaching the validation branches requires a
    real creator. The invalid-input tests below assert on the 400 responses,
    which return before ``start_creation`` is ever called, so no background
    creation (subprocess / network) is started.
    """
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    agent_creator = AgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=None,
        agent_creator=agent_creator,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    return app.test_client()


def test_list_workspaces_returns_known_workspaces(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get("/api/v1/workspaces", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    ids = [w["agent_id"] for w in body["workspaces"]]
    assert str(agent_id) in ids


def test_list_workspaces_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces")

    assert response.status_code == 401


def test_list_workspaces_accepts_session_cookie(tmp_path: Path) -> None:
    # The desktop UI calls the cross-workspace routes with its session cookie
    # (not the bearer), so dual auth must accept a valid signed session cookie.
    agent_id = AgentId()
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(auth_store.get_signing_key()))

    # No bearer header -- only the session cookie.
    response = client.get("/api/v1/workspaces")

    assert response.status_code == 200
    assert str(agent_id) in [w["agent_id"] for w in json.loads(response.data)["workspaces"]]


def test_get_workspace_returns_detail(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["agent_id"] == str(agent_id)


def test_get_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/{other_id}", headers=_auth_header())

    assert response.status_code == 404


def test_malformed_workspace_id_returns_400_not_500(tmp_path: Path) -> None:
    # A malformed id in the path (cannot parse as an AgentId) is a client error:
    # the blueprint maps InvalidRandomIdError to 400 rather than letting it 500.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get("/api/v1/workspaces/not-a-valid-agent-id", headers=_auth_header())

    assert response.status_code == 400
    assert "error" in json.loads(response.data)


def test_workspace_version_returns_original_version_label(tmp_path: Path) -> None:
    # The static resolver has no labels, so original is null and the git-derived
    # fields default to null/[] (no concurrency group is wired in this test).
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/version", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["agent_id"] == str(agent_id)
    assert body["original_minds_version"] is None
    assert body["current_minds_version"] is None
    assert body["upgrade_merges"] == []


def test_workspace_backups_reports_not_found_without_canonical_env(tmp_path: Path) -> None:
    # No restic.env was written for this workspace, so the backups route reports
    # 404 (backups never configured) rather than 500.
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/backups", headers=_auth_header())

    assert response.status_code == 404


def test_create_workspace_without_agent_creator_returns_501(tmp_path: Path) -> None:
    # The default test client has no agent_creator wired, so create is unavailable.
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.post("/api/v1/workspaces", headers=_auth_header(), json={"git_url": "https://example/repo"})

    assert response.status_code == 501


def test_create_workspace_requires_account_id_for_imbue_cloud(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # An IMBUE_CLOUD launch_mode without an account_id must fail validation up
    # front (400), not defer the failure into the background creation thread.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "launch_mode": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    assert "account_id" in json.loads(response.data)["error"]


def test_create_workspace_requires_api_key_for_api_key_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # An API_KEY ai_provider without an anthropic_api_key must fail validation
    # up front (400).
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "ai_provider": "API_KEY"},
    )

    assert response.status_code == 400
    assert "anthropic_api_key" in json.loads(response.data)["error"]


def test_create_workspace_rejects_invalid_backup_provider(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # A malformed backup_provider must fail validation up front (400), before
    # any background creation is started.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "backup_provider": "NOT_A_PROVIDER"},
    )

    assert response.status_code == 400
    assert "backup_provider" in json.loads(response.data)["error"]


def test_create_workspace_rejects_imbue_cloud_backup_without_account(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # imbue_cloud *backups* (independent of the compute/AI provider) need an
    # account; without one the shared backup-request builder rejects it with a
    # 400 that mentions the account, before any background creation starts.
    client = _client_with_agent_creator(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(
        "/api/v1/workspaces",
        headers=_auth_header(),
        json={"git_url": "https://example/repo", "backup_provider": "IMBUE_CLOUD"},
    )

    assert response.status_code == 400
    assert "account" in json.loads(response.data)["error"].lower()


def test_destroy_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.post(f"/api/v1/workspaces/{other_id}/destroy", headers=_auth_header())

    assert response.status_code == 404


def test_lifecycle_without_concurrency_group_returns_501(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/start", headers=_auth_header())

    assert response.status_code == 501


def test_operation_status_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    creation_id = CreationId()

    response = client.get(f"/api/v1/workspaces/operations/{creation_id}", headers=_auth_header())

    assert response.status_code == 404


def test_operation_status_unknown_destroy_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/operations/{other_id}", headers=_auth_header())

    assert response.status_code == 404


def test_establish_ssh_unknown_workspace_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.post(
        f"/api/v1/workspaces/{other_id}/ssh",
        headers=_auth_header(),
        json={"public_key": "ssh-ed25519 AAAA", "requester_workspace_id": "agent-x"},
    )

    assert response.status_code == 404


def test_establish_ssh_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.post(f"/api/v1/workspaces/{agent_id}/ssh", json={})

    assert response.status_code == 401


def test_operation_logs_unknown_create_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    creation_id = CreationId()

    response = client.get(f"/api/v1/workspaces/operations/{creation_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_unknown_destroy_id_returns_404(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.get(f"/api/v1/workspaces/operations/{other_id}/logs", headers=_auth_header())

    assert response.status_code == 404


def test_operation_logs_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.get(f"/api/v1/workspaces/operations/{CreationId()}/logs")

    assert response.status_code == 401
