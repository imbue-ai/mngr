import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from flask.testing import FlaskClient
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_service_log
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import TunnelInfo
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId
from imbue.minds.testing import stub_mngr_host_dir
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


# -- Shared builders for the new routes --


def _build_client(
    tmp_path: Path,
    resolver: BackendResolverInterface,
    *,
    root_concurrency_group: ConcurrencyGroup | None = None,
    mngr_binary: str = "mngr",
    mngr_host_dir: Path | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    session_store: MultiAccountSessionStore | None = None,
    http_client: httpx.Client | None = None,
) -> FlaskClient:
    """Build a desktop-client test client with the /api/v1 surface and the given deps."""
    app = create_desktop_client(
        auth_store=FileAuthStore(data_directory=tmp_path / "auth"),
        backend_resolver=resolver,
        http_client=http_client,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        minds_api_key=_TEST_KEY,
        root_concurrency_group=root_concurrency_group,
        mngr_binary=mngr_binary,
        mngr_host_dir=mngr_host_dir,
        imbue_cloud_cli=imbue_cloud_cli,
        session_store=session_store,
    )
    return app.test_client()


def _write_fake_mngr(directory: Path) -> str:
    """Write an executable fake ``mngr`` that always exits 0; return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "mngr"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return str(script)


class FakeSharingCli(FakeImbueCloudCli):
    """In-memory ``ImbueCloudCli`` double for the sharing routes.

    Returns canned tunnel / service / policy data and records mutating calls,
    so the sharing status/enable/disable routes can be exercised without
    shelling out to ``mngr imbue_cloud``.
    """

    tunnel: TunnelInfo | None = None
    service_entries: list[dict[str, Any]] = Field(default_factory=list)
    service_auth: dict[str, Any] = Field(default_factory=dict)
    removed_services: list[str] = Field(default_factory=list)
    added_services: list[str] = Field(default_factory=list)

    def find_tunnel_for_agent(self, account: str, agent_id: str) -> TunnelInfo | None:
        return self.tunnel

    def create_tunnel(self, *, account: str, agent_id: str, default_policy: Any = None) -> TunnelInfo:
        assert self.tunnel is not None
        return self.tunnel

    def add_service(self, *, account: str, tunnel_name: str, service_name: str, service_url: str) -> dict[str, Any]:
        self.added_services.append(service_name)
        return {}

    def set_service_auth(self, account: str, tunnel_name: str, service_name: str, policy: Any) -> None:
        return None

    def list_services(self, account: str, tunnel_name: str) -> list[dict[str, Any]]:
        return list(self.service_entries)

    def get_service_auth(self, account: str, tunnel_name: str, service_name: str) -> dict[str, Any]:
        return dict(self.service_auth)

    def remove_service(self, account: str, tunnel_name: str, service_name: str) -> None:
        self.removed_services.append(service_name)

    def delete_tunnel(self, account: str, tunnel_name: str) -> None:
        return None


def _associated_session_store(
    tmp_path: Path, cli: FakeSharingCli, agent_id: AgentId, *, user_id: str, email: str
) -> MultiAccountSessionStore:
    """Build a session store with one signed-in account that owns ``agent_id``."""
    cli.add_account(user_id=user_id, email=email)
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    store.associate_workspace(user_id, str(agent_id))
    return store


# -- PATCH /api/v1/workspaces/<id> (color + account) --


def test_patch_workspace_color_success(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    fake_mngr = _write_fake_mngr(tmp_path / "bin")
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group, mngr_binary=fake_mngr)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "#fff"})

    assert response.status_code == 200
    assert json.loads(response.data)["color"] == "#ffffff"
    # The optimistic local update is reflected in the resolver snapshot.
    assert resolver.get_workspace_color(agent_id) == "#ffffff"


def test_patch_workspace_color_invalid_hex(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "not-a-color"})

    assert response.status_code == 400
    assert json.loads(response.data)["error"] == "invalid_hex"


def test_patch_workspace_color_not_primary(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())
    other_id = AgentId()

    response = client.patch(f"/api/v1/workspaces/{other_id}", headers=_auth_header(), json={"color": "#abcdef"})

    assert response.status_code == 404
    assert json.loads(response.data)["error"] == "not_primary"


def test_patch_workspace_color_host_unreachable_without_concurrency_group(tmp_path: Path) -> None:
    # A known workspace with no concurrency group wired cannot run mngr label.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"color": "#abcdef"})

    assert response.status_code == 502
    assert json.loads(response.data)["error"] == "host_unreachable"


def test_patch_workspace_associate_account(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "11111111-1111-1111-1111-111111111111"
    cli.add_account(user_id=user_id, email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": user_id})

    assert response.status_code == 200
    assert json.loads(response.data)["account_id"] == user_id
    account = store.get_account_for_workspace(str(agent_id))
    assert account is not None and str(account.email) == "owner@example.com"


def test_patch_workspace_disassociate_account_with_null(tmp_path: Path) -> None:
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    cli = _fake_sharing_cli()
    user_id = "22222222-2222-2222-2222-222222222222"
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email="owner@example.com")
    client = _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", headers=_auth_header(), json={"account_id": None})

    assert response.status_code == 200
    assert json.loads(response.data)["account_id"] is None
    assert store.get_account_for_workspace(str(agent_id)) is None


def test_patch_workspace_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.patch(f"/api/v1/workspaces/{agent_id}", json={"color": "#fff"})

    assert response.status_code == 401


# -- DELETE /api/v1/workspaces/operations/<id> (dismiss) --


def test_dismiss_create_operation_is_idempotent_noop(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/{CreationId()}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {}


def test_dismiss_destroy_operation_is_idempotent_noop(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/{AgentId()}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {}


def test_dismiss_operation_requires_bearer(tmp_path: Path) -> None:
    client = _client_with_workspace(tmp_path, AgentId())

    response = client.delete(f"/api/v1/workspaces/operations/{AgentId()}")

    assert response.status_code == 401


# -- Desktop provider toggle --


def test_patch_provider_enable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_path = stub_mngr_host_dir(monkeypatch, tmp_path, "minds-dev-tname")
    monkeypatch.setenv(MINDS_ROOT_NAME_ENV_VAR, "minds-dev-tname")
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    client = _build_client(tmp_path, resolver)

    response = client.patch("/api/v1/desktop/providers/docker", headers=_auth_header(), json={"enabled": True})

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body == {"provider_name": "docker", "enabled": True, "changed": True}
    assert "is_enabled = true" in settings_path.read_text()


def test_patch_provider_disable_with_active_workspaces_conflicts(tmp_path: Path) -> None:
    # The single workspace is served by provider "local" and its host is not
    # DESTROYED, so disabling "local" must be rejected with a 409 and no write.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.patch("/api/v1/desktop/providers/local", headers=_auth_header(), json={"enabled": False})

    assert response.status_code == 409
    assert "active workspace" in json.loads(response.data)["error"].lower()


def test_patch_provider_requires_enabled_bool(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.patch("/api/v1/desktop/providers/docker", headers=_auth_header(), json={})

    assert response.status_code == 400


def test_patch_provider_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.patch("/api/v1/desktop/providers/docker", json={"enabled": True})

    assert response.status_code == 401


# -- Desktop running-workspaces / stop-hosts / state-container --


def test_desktop_running_workspaces(tmp_path: Path) -> None:
    # The lone "local"-provider workspace is not on a shutdown-capable backend,
    # so no workspaces are reported as running, but the route returns the shape.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver)

    response = client.get("/api/v1/desktop/running-workspaces", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"running": []}


def test_desktop_stop_hosts_without_concurrency_group_returns_503(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.post("/api/v1/desktop/stop-hosts", headers=_auth_header())

    assert response.status_code == 503


def test_desktop_stop_hosts_returns_still_running(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    # No system-services sibling is resolvable for the lone workspace, so nothing
    # is stopped and the (empty) still-running set is returned.
    agent_id = AgentId()
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, root_concurrency_group=root_concurrency_group)

    response = client.post(f"/api/v1/desktop/stop-hosts?agent_id={agent_id}", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"still_running": []}


def test_desktop_stop_state_container_without_concurrency_group(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.post("/api/v1/desktop/state-container/stop", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"stopped": False}


def test_desktop_stop_state_container_no_profile_reports_not_stopped(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    # With a mngr host dir that has no profile, no container can be resolved, so
    # the stop is a no-op (stopped=False) and never touches Docker.
    client = _build_client(
        tmp_path,
        StaticBackendResolver(url_by_agent_and_service={}),
        root_concurrency_group=root_concurrency_group,
        mngr_host_dir=tmp_path / "empty-host",
    )

    response = client.post("/api/v1/desktop/state-container/stop", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data) == {"stopped": False}


def test_desktop_running_workspaces_requires_bearer(tmp_path: Path) -> None:
    client = _build_client(tmp_path, StaticBackendResolver(url_by_agent_and_service={}))

    response = client.get("/api/v1/desktop/running-workspaces")

    assert response.status_code == 401


# -- Sharing sub-resource --


def _sharing_client(
    tmp_path: Path,
    agent_id: AgentId,
    cli: FakeSharingCli,
    *,
    user_id: str = "33333333-3333-3333-3333-333333333333",
    email: str = "owner@example.com",
    service_logs: dict[str, str] | None = None,
    mngr_binary: str = "mngr",
) -> FlaskClient:
    resolver = make_resolver_with_data(make_agents_json(agent_id), service_logs=service_logs)
    store = _associated_session_store(tmp_path, cli, agent_id, user_id=user_id, email=email)
    return _build_client(tmp_path, resolver, imbue_cloud_cli=cli, session_store=store, mngr_binary=mngr_binary)


def _fake_sharing_cli(tunnel: TunnelInfo | None = None, **kwargs: Any) -> FakeSharingCli:
    return FakeSharingCli(
        parent_concurrency_group=ConcurrencyGroup(name="fake-sharing-cli"),
        connector_url=FAKE_CONNECTOR_URL,
        tunnel=tunnel,
        **kwargs,
    )


def test_sharing_status_enabled(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", services=("web",)),
        service_entries=[{"service_name": "web", "hostname": "share.example.com"}],
        service_auth={"emails": ["owner@example.com"]},
    )
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert body["url"] == "https://share.example.com"
    assert body["policy"]["emails"] == ["owner@example.com"]


def test_sharing_status_disabled_when_no_tunnel(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(tunnel=None)
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False


def test_sharing_enable_returns_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(
        tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", token=SecretStr("token"), services=("web",))
    )
    # The tunnel-token injection shells out to `mngr exec` (resolved via PATH);
    # a fake mngr on PATH keeps that a fast no-op.
    fake_mngr_dir = tmp_path / "bin"
    _write_fake_mngr(fake_mngr_dir)
    monkeypatch.setenv("PATH", f"{fake_mngr_dir}{os.pathsep}{os.environ['PATH']}")
    client = _sharing_client(
        tmp_path,
        agent_id,
        cli,
        service_logs={str(agent_id): make_service_log("web", "http://127.0.0.1:9000")},
    )

    response = client.put(
        f"/api/v1/workspaces/{agent_id}/sharing/web",
        headers=_auth_header(),
        json={"emails": ["viewer@example.com"]},
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["enabled"] is True
    assert "web" in cli.added_services


def test_sharing_disable_returns_json(tmp_path: Path) -> None:
    agent_id = AgentId()
    cli = _fake_sharing_cli(tunnel=TunnelInfo(tunnel_name="tn", tunnel_id="ti", services=("web",)))
    client = _sharing_client(tmp_path, agent_id, cli)

    response = client.delete(f"/api/v1/workspaces/{agent_id}/sharing/web", headers=_auth_header())

    assert response.status_code == 200
    assert json.loads(response.data)["enabled"] is False
    assert "web" in cli.removed_services


def test_sharing_status_requires_bearer(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(f"/api/v1/workspaces/{agent_id}/sharing/web")

    assert response.status_code == 401


def test_sharing_readiness_reports_ready_on_access_redirect(tmp_path: Path) -> None:
    agent_id = AgentId()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://team.cloudflareaccess.com/login"})

    http_client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    resolver = make_resolver_with_data(make_agents_json(agent_id))
    client = _build_client(tmp_path, resolver, http_client=http_client)

    response = client.get(
        f"/api/v1/workspaces/{agent_id}/sharing/web/readiness?url=https://share.example.com",
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"ready": True}


def test_sharing_readiness_not_ready_without_http_client(tmp_path: Path) -> None:
    agent_id = AgentId()
    client = _client_with_workspace(tmp_path, agent_id)

    response = client.get(
        f"/api/v1/workspaces/{agent_id}/sharing/web/readiness?url=https://share.example.com",
        headers=_auth_header(),
    )

    assert response.status_code == 200
    assert json.loads(response.data) == {"ready": False}
