"""Integration tests for the app-level settings permissions routes."""

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
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
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
            "permissions": [{"name": "slack-read-all"}, {"name": "slack-write-all"}],
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


class _UnavailableGatewayClient(FakeLatchkeyGatewayClient):
    """Fake whose reads fail, standing in for a down latchkey gateway."""

    def get_permission_rules(self, permissions_file_path: Path) -> dict[str, tuple[str, ...]]:
        raise LatchkeyGatewayClientError("gateway down")


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


def test_settings_page_lists_granted_service_per_workspace(tmp_path: Path) -> None:
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
    assert "Slack" in body
    assert "My Workspace" in body
    assert "slack-read-all" in body
    assert 'data-service-name="slack"' in body


def test_settings_page_empty_state_when_no_grants(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    assert "No third-party service permissions" in response.text


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


def test_settings_page_shows_unavailable_notice_when_gateway_down(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, gateway_client=_UnavailableGatewayClient())
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    assert "gateway is unavailable" in response.text
    assert "No third-party service permissions" not in response.text


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
