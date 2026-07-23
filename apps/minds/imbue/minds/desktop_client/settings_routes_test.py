"""Integration tests for the app-level settings permissions routes."""

import threading
import time
from collections.abc import Callable
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
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
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


class _UnavailableGatewayClient(FakeLatchkeyGatewayClient):
    """Fake whose reads fail, standing in for a down latchkey gateway."""

    def get_permission_rules(self, permissions_file_path: Path) -> dict[str, tuple[str, ...]]:
        raise LatchkeyGatewayClientError("gateway down")


class _ConnectorLatchkey(Latchkey):
    """``Latchkey`` double for the connector-account routes.

    ``services_info`` reports the configured accounts, ``add_account`` records
    its calls and returns a configurable result, and ``auth_clear`` records each
    call and drops the named account so a follow-up ``services_info`` reflects
    the change (letting :func:`disconnect_account` detect the last account).
    """

    accounts_by_service: dict[str, list[str]] = Field(default_factory=dict)
    add_account_calls: list[str] = Field(default_factory=list)
    add_account_result: tuple[bool, str] = Field(default=(True, ""))
    cleared_calls: list[tuple[str, str | None]] = Field(default_factory=list)

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        del is_offline
        accounts = tuple(
            ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
            for account in self.accounts_by_service.get(service_name, [])
        )
        return LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID if accounts else CredentialStatus.MISSING,
            accounts=accounts,
            auth_options=frozenset({"browser", "set"}),
            set_credentials_example=None,
        )

    def add_account(self, service_name: str) -> tuple[bool, str]:
        self.add_account_calls.append(service_name)
        return self.add_account_result

    def auth_clear(
        self,
        service_name: str,
        *,
        account: str | None = None,
        is_all: bool = False,
    ) -> tuple[bool, str]:
        del is_all
        self.cleared_calls.append((service_name, account))
        if account is not None and service_name in self.accounts_by_service:
            self.accounts_by_service[service_name] = [
                stored for stored in self.accounts_by_service[service_name] if stored != account
            ]
        return (True, "")


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses (for background work)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


def _build_handler(
    tmp_path: Path,
    gateway_client: FakeLatchkeyGatewayClient | None = None,
    latchkey: Latchkey | None = None,
) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey or Latchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent"),
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
    # The per-permission description is surfaced as a tooltip on the pill.
    assert 'data-tooltip="All read operations across the Slack API."' in body
    # The service section carries a per-service revoke-all action and a workspace count.
    assert "Revoke all" in body
    assert "1 workspace" in body


def test_settings_page_shows_plural_workspace_count(tmp_path: Path) -> None:
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    for host in (host_a, host_b):
        save_permissions(
            permissions_path_for_host(_plugin_dir(tmp_path), host),
            LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
        )
    handler = _build_handler(tmp_path)
    client = _build_client(
        tmp_path, handler, {agent_a: str(host_a), agent_b: str(host_b)}, {agent_a: "A", agent_b: "B"}
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "2 workspaces" in response.text


def test_settings_sidebar_groups_nav_into_sections(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    nav = response.text.split("Settings sections")[1].split("</nav>")[0]
    # The two group eyebrows and the compact nav labels.
    assert 'type-section text-tertiary px-2 mb-1">Permissions' in nav
    assert 'type-section text-tertiary px-2 mt-4 mb-1">Other' in nav
    for label in ("Connectors", "Local files", "Workspaces", "Error reporting", "Master password"):
        assert label in nav
    # The switchable entries are the nav buttons (the eyebrows are not buttons).
    assert nav.count("data-settings-nav=") == 5


def test_settings_page_empty_state_when_no_grants(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    # Each category now has its own empty state.
    assert "No connectors have been added yet." in response.text


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
    assert "can't be loaded right now" in response.text
    assert "No connectors have been added yet." not in response.text


# -- Connector accounts --------------------------------------------------------


def test_settings_page_lists_service_accounts_and_add_button(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    save_permissions(
        permissions_path_for_host(_plugin_dir(tmp_path), host),
        LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)),
    )
    latchkey = _ConnectorLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["hynek@imbue-ai", "hynek@glebs-corner"]},
    )
    handler = _build_handler(tmp_path, latchkey=latchkey)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    body = response.text
    assert "+ Add account" in body
    assert "hynek@imbue-ai" in body
    assert "hynek@glebs-corner" in body
    assert "Disconnect" in body
    assert 'data-account="hynek@imbue-ai"' in body
    assert "Allowed on all accounts:" in body


def test_add_account_invokes_latchkey_add_account(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    latchkey = _ConnectorLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    handler = _build_handler(tmp_path, latchkey=latchkey)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/connectors/add-account", json={"service_name": "slack"})

    assert response.status_code == 200
    assert latchkey.add_account_calls == ["slack"]


def test_add_account_reports_failure_as_502(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    latchkey = _ConnectorLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        add_account_result=(False, "user cancelled"),
    )
    handler = _build_handler(tmp_path, latchkey=latchkey)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/connectors/add-account", json={"service_name": "slack"})

    assert response.status_code == 502
    assert "user cancelled" in response.text


def test_add_account_missing_service_name_returns_400(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, latchkey=_ConnectorLatchkey(latchkey_directory=tmp_path, latchkey_binary="/x"))
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/connectors/add-account", json={})

    assert response.status_code == 400


def test_disconnect_account_clears_but_keeps_grants_when_accounts_remain(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    path = permissions_path_for_host(_plugin_dir(tmp_path), host)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    latchkey = _ConnectorLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["a@x", "b@x"]},
    )
    handler = _build_handler(tmp_path, latchkey=latchkey)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.post("/settings/connectors/disconnect-account", json={"service_name": "slack", "account": "a@x"})

    assert response.status_code == 200
    assert latchkey.cleared_calls == [("slack", "a@x")]
    # An account still remains, so the service's grants are left in place.
    assert handler.gateway_client.get_permission_rules(path) == {"slack-api": ("slack-read-all",)}


def test_disconnect_last_account_revokes_grants_across_workspaces(tmp_path: Path) -> None:
    agent_a, host_a = str(AgentId()), HostId()
    agent_b, host_b = str(AgentId()), HostId()
    path_a = permissions_path_for_host(_plugin_dir(tmp_path), host_a)
    path_b = permissions_path_for_host(_plugin_dir(tmp_path), host_b)
    save_permissions(path_a, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-read-all"]},)))
    save_permissions(path_b, LatchkeyPermissionsConfig(rules=({"slack-api": ["slack-write-all"]},)))
    latchkey = _ConnectorLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["only@x"]},
    )
    handler = _build_handler(tmp_path, latchkey=latchkey)
    client = _build_client(
        tmp_path, handler, {agent_a: str(host_a), agent_b: str(host_b)}, {agent_a: "A", agent_b: "B"}
    )

    response = client.post(
        "/settings/connectors/disconnect-account", json={"service_name": "slack", "account": "only@x"}
    )

    assert response.status_code == 200
    assert latchkey.cleared_calls == [("slack", "only@x")]
    # Disconnecting the last account triggers the background "revoke all", which
    # strips the service's grants from every workspace host.
    assert _wait_until(lambda: handler.gateway_client.get_permission_rules(path_a) == {})
    assert _wait_until(lambda: handler.gateway_client.get_permission_rules(path_b) == {})


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


def test_settings_page_lists_file_sharing_section(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    _seed_file_sharing(tmp_path, host, read_paths=("/home/docs",), write_paths=("/home/out",))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "My Workspace"})

    response = client.get("/settings")

    assert response.status_code == 200
    body = response.text
    assert "File sharing" in body
    assert "read and write" in body
    # The shared paths are surfaced as the chip tooltip.
    assert 'data-tooltip="/home/docs"' in body
    assert 'data-tooltip="/home/out"' in body


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


def test_settings_page_lists_workspace_delegation_by_granting_workspace(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    target = str(AgentId())
    _seed_workspace_ops(tmp_path, host, ("minds-workspaces-read", f"minds-workspaces-ssh-{target}"))
    handler = _build_handler(tmp_path)
    client = _build_client(tmp_path, handler, {agent: str(host)}, {agent: "Ops Bot"})

    response = client.get("/settings")

    assert response.status_code == 200
    body = response.text
    assert "Workspace delegation" in body
    # Grouped by the granting workspace (its name is the group heading).
    assert "Ops Bot" in body
    # One row per verb, each with its own revoke, keyed by the verb schema name.
    assert ">read</code>" in body and ">ssh</code>" in body
    assert 'data-verb-permission="minds-workspaces-read"' in body
    assert 'data-verb-permission="minds-workspaces-ssh"' in body
    # ``read`` is all-workspaces; ``ssh`` names the specific target.
    assert "All workspaces" in body
    assert target in body


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
