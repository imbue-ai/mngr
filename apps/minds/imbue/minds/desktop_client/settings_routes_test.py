"""Integration tests for the permission revoke routes and the app-level settings page."""

from pathlib import Path

from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all"},
            ],
        },
    ],
}


class _WorkspaceResolver(StaticBackendResolver):
    """Static resolver that reports active workspaces mapped to hosts, with names."""

    host_by_agent: dict[str, str] = Field(default_factory=dict)
    name_by_agent: dict[str, str] = Field(default_factory=dict)

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(a) for a in self.host_by_agent)

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(a) for a in self.host_by_agent)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host = self.host_by_agent.get(str(agent_id))
        if host is None:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.name_by_agent.get(str(agent_id))


def _build_handler(
    tmp_path: Path,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent"),
        services_catalog=ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD),
        mngr_message_sender=MngrMessageSender(
            mngr_caller=RecordingMngrCaller(),
            concurrency_group=ConcurrencyGroup(name="settings-routes-test-unused"),
        ),
        gateway_client=gateway_client or build_fake_gateway_client(),
    )


def _build_client(
    tmp_path: Path,
    handler: LatchkeyPermissionGrantHandler,
    host_by_agent: dict[str, str],
    name_by_agent: dict[str, str],
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        host_by_agent=host_by_agent,
        name_by_agent=name_by_agent,
    )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
        request_inbox=RequestInbox(),
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _plugin_dir(tmp_path: Path) -> Path:
    return Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent").plugin_data_dir


def test_settings_page_lists_granted_connector(tmp_path: Path) -> None:
    """The app-level Settings page hosts the permission sections: a granted
    connector shows up with its workspace and permissions, alongside the
    device settings (error reporting, backup password)."""
    agent, host = str(AgentId()), HostId()
    save_permissions(
        permissions_path_for_host(_plugin_dir(tmp_path), host),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    body = response.text
    # The permission sections are back on the app-level settings page.
    for section in ("Connectors", "Local files", "Workspaces", "Error reporting", "Backup password"):
        assert section in body
    # The granted connector renders with its workspace + permission label.
    assert "Slack" in body
    assert "My Workspace" in body
    assert "slack-read-all" in body
    assert 'data-service-name="slack"' in body
    # The full page keeps its "back to workspaces" link (the modal drops it).
    assert "Back to workspaces" in body


def test_settings_modal_lists_granted_connector_without_back_link(tmp_path: Path) -> None:
    """The centered settings modal renders the same permission sections as the
    full page, minus the "back to workspaces" link."""
    agent, host = str(AgentId()), HostId()
    save_permissions(
        permissions_path_for_host(_plugin_dir(tmp_path), host),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings/modal")

    assert response.status_code == 200
    body = response.text
    assert "Connectors" in body
    assert "slack-read-all" in body
    assert 'data-service-name="slack"' in body
    assert "Back to workspaces" not in body
    assert 'id="settings-modal-backdrop"' in body


def test_revoke_service_for_workspace_removes_rule(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    path = permissions_path_for_host(_plugin_dir(tmp_path), host)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post(
        "/settings/permissions/revoke",
        json={"workspace_agent_id": agent, "service_name": "slack"},
    )

    assert response.status_code == 200
    assert handler.gateway_client.get_permission_rules(path) == {}


def test_revoke_all_removes_rule_across_workspaces(tmp_path: Path) -> None:
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    path_a = permissions_path_for_host(_plugin_dir(tmp_path), host_a)
    path_b = permissions_path_for_host(_plugin_dir(tmp_path), host_b)
    save_permissions(path_a, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    save_permissions(path_b, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-write-all"]},)))
    handler = _build_handler(tmp_path)
    client = _build_client(
        tmp_path, handler, {agent_a: str(host_a), agent_b: str(host_b)}, {agent_a: "A", agent_b: "B"}
    )

    response = client.post("/settings/permissions/revoke-all", json={"service_name": "slack"})

    assert response.status_code == 200
    assert handler.gateway_client.get_permission_rules(path_a) == {}
    assert handler.gateway_client.get_permission_rules(path_b) == {}


def test_revoke_unknown_service_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post(
        "/settings/permissions/revoke",
        json={"workspace_agent_id": agent, "service_name": "nope"},
    )

    assert response.status_code == 400


def test_revoke_missing_fields_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/permissions/revoke", json={"service_name": "slack"})

    assert response.status_code == 400


# -- File sharing --------------------------------------------------------------

_BASELINE_SELF_PERM = "latchkey-self-create-permission-request"


def _seed_file_sharing(
    tmp_path: Path, host: HostId, read_paths: tuple[str, ...], write_paths: tuple[str, ...]
) -> Path:
    perms = [_BASELINE_SELF_PERM]
    perms += [f"minds-file-server-read-{p}" for p in read_paths]
    perms += [f"minds-file-server-write-{p}" for p in write_paths]
    path = permissions_path_for_host(_plugin_dir(tmp_path), host)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"latchkey-self": perms},)))
    return path


def test_revoke_file_sharing_for_workspace_keeps_other_permissions(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    path = _seed_file_sharing(tmp_path, host, read_paths=("/home/docs",), write_paths=("/home/out",))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/permissions/file-sharing/revoke", json={"workspace_agent_id": agent})

    assert response.status_code == 200
    assert handler.gateway_client.get_permission_rules(path)["latchkey-self"] == (_BASELINE_SELF_PERM,)


def test_revoke_file_sharing_all_removes_across_workspaces(tmp_path: Path) -> None:
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    path_a = _seed_file_sharing(tmp_path, host_a, read_paths=("/a",), write_paths=())
    path_b = _seed_file_sharing(tmp_path, host_b, read_paths=(), write_paths=("/b",))
    handler = _build_handler(tmp_path)
    client = _build_client(
        tmp_path, handler, {agent_a: str(host_a), agent_b: str(host_b)}, {agent_a: "A", agent_b: "B"}
    )

    response = client.post("/settings/permissions/file-sharing/revoke-all", json={})

    assert response.status_code == 200
    assert handler.gateway_client.get_permission_rules(path_a)["latchkey-self"] == (_BASELINE_SELF_PERM,)
    assert handler.gateway_client.get_permission_rules(path_b)["latchkey-self"] == (_BASELINE_SELF_PERM,)


def test_revoke_file_sharing_missing_workspace_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/permissions/file-sharing/revoke", json={})

    assert response.status_code == 400


def test_revoke_file_sharing_requires_authentication(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})
    client.delete_cookie(SESSION_COOKIE_NAME)

    response = client.post("/settings/permissions/file-sharing/revoke-all", json={})

    assert response.status_code == 403


# -- Cross-workspace management ------------------------------------------------


def _seed_workspace_ops(tmp_path: Path, host: HostId, names: tuple[str, ...]) -> Path:
    path = permissions_path_for_host(_plugin_dir(tmp_path), host)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"latchkey-self": [_BASELINE_SELF_PERM, *names]},)))
    return path


def test_revoke_workspace_delegation_verb_keeps_other_verbs(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    path = _seed_workspace_ops(tmp_path, host, ("minds-workspaces-read", f"minds-workspaces-ssh-{target}"))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "Ops Bot"})

    response = client.post(
        "/settings/permissions/workspace/revoke",
        json={"workspace_agent_id": agent, "verb": "minds-workspaces-ssh"},
    )

    assert response.status_code == 200
    remaining = handler.gateway_client.get_permission_rules(path)["latchkey-self"]
    assert f"minds-workspaces-ssh-{target}" not in remaining
    assert "minds-workspaces-read" in remaining


def test_revoke_workspace_delegation_unknown_verb_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    _seed_workspace_ops(tmp_path, host, ("minds-workspaces-read",))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "Ops Bot"})

    response = client.post(
        "/settings/permissions/workspace/revoke",
        json={"workspace_agent_id": agent, "verb": "minds-workspaces-nope"},
    )

    assert response.status_code == 400


def test_revoke_workspace_delegation_missing_fields_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "Ops Bot"})

    response = client.post("/settings/permissions/workspace/revoke", json={"workspace_agent_id": agent})

    assert response.status_code == 400


def test_revoke_requires_authentication(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})
    client.delete_cookie(SESSION_COOKIE_NAME)

    response = client.post(
        "/settings/permissions/revoke",
        json={"workspace_agent_id": agent, "service_name": "slack"},
    )

    assert response.status_code == 403
