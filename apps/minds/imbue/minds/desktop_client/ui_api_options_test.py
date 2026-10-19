"""Unit tests for the T3 /ui/api options-data endpoint and its pure helpers."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import ServiceShareKind
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.imbue_cloud_cli import MachineSizeCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_options import _workspace_host_coordinate_for_options
from imbue.minds.desktop_client.ui_api_options import accepted_service_icon
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import DiscoveredHostSize
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_AGENT_ID = "agent-" + "a" * 32
_HOST_ID = "host-" + "f" * 32


class _OptionsSeededResolver(StaticBackendResolver):
    """Static resolver carrying the display info, labels, icons, display names, and share kinds the options reads."""

    display_info_by_agent_id: dict[str, AgentDisplayInfo] = Field(default_factory=dict, frozen=True)
    color_by_agent_id: dict[str, str] = Field(default_factory=dict, frozen=True)
    labels_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)
    icons_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)
    display_names_by_agent_id: dict[str, dict[str, str]] = Field(default_factory=dict, frozen=True)
    share_kinds_by_agent_id: dict[str, dict[str, ServiceShareKind]] = Field(default_factory=dict, frozen=True)
    errored_providers: tuple[str, ...] = Field(default=(), frozen=True)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        return self.display_info_by_agent_id.get(str(agent_id))

    def get_workspace_color(self, agent_id: AgentId) -> str | None:
        return self.color_by_agent_id.get(str(agent_id))

    def list_service_labels_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.labels_by_agent_id.get(str(agent_id), {}))

    def list_service_icons_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.icons_by_agent_id.get(str(agent_id), {}))

    def list_service_display_names_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.display_names_by_agent_id.get(str(agent_id), {}))

    def list_service_share_kinds_for_agent(self, agent_id: AgentId) -> dict:
        return dict(self.share_kinds_by_agent_id.get(str(agent_id), {}))

    def get_provider_errors(self) -> dict:
        return {ProviderInstanceName(name): object() for name in self.errored_providers}


def _seeded_resolver() -> _OptionsSeededResolver:
    return _OptionsSeededResolver(
        url_by_agent_and_service={
            _AGENT_ID: {
                "system_interface": "http://127.0.0.1:9001",
                "web": "http://127.0.0.1:9002",
                "terminal": "http://127.0.0.1:9003",
                "bad_name": "http://127.0.0.1:9004",
            }
        },
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id=_HOST_ID, provider_name="local")
        },
        color_by_agent_id={_AGENT_ID: "#9fbbd3"},
        labels_by_agent_id={_AGENT_ID: {"web": "web-r4nd", "system_interface": "shell-r4nd"}},
        display_names_by_agent_id={_AGENT_ID: {"web": "Web", "system_interface": "Workspace"}},
    )


def test_options_data_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 401


def test_options_data_rejects_malformed_agent_ids(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get("/ui/api/workspaces/not-an-agent-id/options")

    assert response.status_code == 404


def test_options_data_returns_workspace_context(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 200
    data = json.loads(response.get_data(as_text=True))
    assert data["agent_id"] == _AGENT_ID
    assert data["host_id"] == _HOST_ID
    assert data["name"] == "sunny"
    assert data["color"] == "#9fbbd3"
    assert data["palette"] == dict(WORKSPACE_PALETTE)
    assert data["is_stale"] is False
    assert data["leased_owner_email"] == ""
    assert data["is_account_owned_provider"] is False
    # No session store in this minimal app: unassociated, no accounts offered.
    assert data["has_account"] is False
    assert data["account_email"] == ""
    assert data["account_display_name"] is None
    assert data["account_profile_picture_url"] is None
    assert data["accounts"] == []
    # The shell is no per-app target, and the terminal is a system app, listed
    # apart. A name that could never be a hostname label is a target like any
    # other: the link comes from the service's own origin label, not its name.
    assert data["app_services"] == ["bad_name", "web"]
    assert data["system_services"] == ["terminal"]
    assert data["service_labels"] == {"web": "web-r4nd", "system_interface": "shell-r4nd"}
    # Never the shell's: the whole-machine target is named for what it grants.
    assert data["service_display_names"] == {"web": "Web"}
    assert data["whole_service"] == "system_interface"
    assert data["ssh_command"] == ""


def test_options_data_carries_the_ssh_command_for_a_remote_machine(tmp_path: Path) -> None:
    base = _seeded_resolver()
    resolver = base.model_copy_update(
        to_update(
            base.field_ref().ssh_info_by_agent_id,
            {_AGENT_ID: RemoteSSHInfo(user="root", host="203.0.113.7", port=2222, key_path=Path("/k/id"))},
        )
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    data = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/options").get_data(as_text=True))

    assert data["ssh_command"] == "ssh -i /k/id -p 2222 root@203.0.113.7"


def test_options_data_carries_the_owning_accounts_name_and_profile_picture(tmp_path: Path) -> None:
    # The Share tab's owner row renders from these fields alone (no connector
    # fetch), so the payload must carry exactly what the signed-in account says.
    cli = FakeImbueCloudCli(connector_url=FAKE_CONNECTOR_URL)
    user_id = "44444444-4444-4444-4444-444444444444"
    cli.add_account(
        user_id=user_id,
        email="owner@example.com",
        display_name="Owner Person",
        profile_picture_url="https://pictures.example/owner.png",
    )
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        user_id=user_id, agent_id=_AGENT_ID, host_id=_HOST_ID, display_name="", color=None, is_cloud_row=False
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=_seeded_resolver(),
        imbue_cloud_cli=cli,
        session_store=session_store,
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 200
    data = json.loads(response.get_data(as_text=True))
    assert data["has_account"] is True
    assert data["account_email"] == "owner@example.com"
    assert data["account_display_name"] == "Owner Person"
    assert data["account_profile_picture_url"] == "https://pictures.example/owner.png"


def test_options_data_carries_what_the_panel_refuses_a_grant_by(tmp_path: Path) -> None:
    # The share panel refuses an entry before it becomes a row, so it needs the
    # public-provider list and the granter's own address.
    cli = FakeImbueCloudCli(connector_url=FAKE_CONNECTOR_URL)
    user_id = "55555555-5555-5555-5555-555555555555"
    cli.add_account(user_id=user_id, email="owner@example.com")
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        user_id=user_id, agent_id=_AGENT_ID, host_id=_HOST_ID, display_name="", color=None, is_cloud_row=False
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=_seeded_resolver(),
        imbue_cloud_cli=cli,
        session_store=session_store,
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    assert response.status_code == 200
    data = json.loads(response.get_data(as_text=True))
    assert "gmail.com" in data["public_email_domains"]
    assert "example.com" not in data["public_email_domains"]
    # Sorted, so a diff of the payload is stable.
    assert data["public_email_domains"] == sorted(data["public_email_domains"])
    assert data["account_email"] == "owner@example.com"


def test_options_data_flags_stale_and_leased_workspaces(tmp_path: Path) -> None:
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={_AGENT_ID: {}},
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="leased", host_id=_HOST_ID, provider_name="imbue_cloud_alice")
        },
        errored_providers=("imbue_cloud_alice",),
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    data = json.loads(response.get_data(as_text=True))
    assert data["is_stale"] is True
    # No linked account and no signed-in account's provider hosts it: the owner is not known.
    assert data["leased_owner_email"] == ""
    assert data["is_account_owned_provider"] is True
    # No stored color label: the default is reported.
    assert data["color"] == DEFAULT_WORKSPACE_COLOR


def _leased_machine_options_data(
    tmp_path: Path,
    hosting_user_id: str,
    cli: FakeImbueCloudCli,
    session_store: MultiAccountSessionStore,
    provider_name: str | None = None,
    connection_store: ConnectionStore | None = None,
) -> dict[str, Any]:
    """The options data for a machine on ``provider_name`` (default: ``hosting_user_id``'s Imbue Cloud instance)."""
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={_AGENT_ID: {}},
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(
                agent_name="leased",
                host_id=_HOST_ID,
                provider_name=provider_name or imbue_cloud_provider_name_for_user(hosting_user_id),
            )
        },
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        imbue_cloud_cli=cli,
        session_store=session_store,
        connection_store=connection_store,
    )
    return json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/options").get_data(as_text=True))


def test_options_data_names_a_leased_machines_owner_from_its_provider_when_it_has_no_link(tmp_path: Path) -> None:
    """The owner line a leased machine shows needs no association record: the provider instance
    it runs under is the one Imbue Studio registered for its account."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-bob", email="bob@example.com")
    cli.add_account(user_id="user-alice", email="alice@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)

    data = _leased_machine_options_data(tmp_path, "user-alice", cli, session_store)
    assert data["current_account"] is None
    assert data["leased_owner_email"] == "alice@example.com"


def test_options_data_names_the_owner_of_a_machine_on_an_account_owned_connection(tmp_path: Path) -> None:
    """A machine on a connection instance belongs to the signed-in account that owns the connection."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-bob", email="bob@example.com")
    cli.add_account(user_id="user-alice", email="alice@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    connection_store = make_connection_store_for_test(tmp_path / "connections", cli)
    unlock_account_with_master_password_for_test(connection_store.paths, "user-alice")
    connection_id = add_modal_connection_for_test(connection_store, "user-alice", "team modal")
    instance_name = provider_instance_name_for_connection(ConnectionBackend.MODAL, connection_id)

    data = _leased_machine_options_data(
        tmp_path, "user-bob", cli, session_store, provider_name=instance_name, connection_store=connection_store
    )
    assert data["current_account"] is None
    assert data["leased_owner_email"] == "alice@example.com"
    assert data["is_account_owned_provider"] is True


def test_options_data_names_a_leased_machines_linked_account_as_its_owner(tmp_path: Path) -> None:
    """A stored account link names the owner even when another signed-in account's provider matches."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-owner", email="owner@example.com")
    cli.add_account(user_id="user-other", email="other@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    session_store.associate_created_workspace(
        user_id="user-owner", agent_id=_AGENT_ID, host_id=_HOST_ID, display_name="", color=None, is_cloud_row=True
    )

    data = _leased_machine_options_data(tmp_path, "user-other", cli, session_store)
    assert data["leased_owner_email"] == "owner@example.com"


def test_options_data_serves_only_gate_passing_app_icons(tmp_path: Path) -> None:
    # The icons are workspace-authored markup headed for the trusted shell's
    # DOM: only well-shaped, non-executable svg reaches the payload, and only
    # for the rendered app targets.
    good_icon = '<svg viewBox="0 0 24 24"><path d="M2 2h20"/></svg>'
    base = _seeded_resolver()
    resolver = base.model_copy_update(
        to_update(
            base.field_ref().icons_by_agent_id,
            {
                _AGENT_ID: {
                    "web": good_icon,
                    "terminal": good_icon,
                    "system_interface": '<svg onload="x()"></svg>',
                }
            },
        )
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    data = json.loads(response.get_data(as_text=True))
    # system_interface's markup fails the gate; the terminal, a system app, is
    # still listed, so its icon is served beside web's.
    assert data["service_icons"] == {"web": good_icon, "terminal": good_icon}


def test_options_data_share_targets_follow_the_workspaces_announced_share_kinds(tmp_path: Path) -> None:
    # An announcement decides over the legacy name lists, whichever way it goes.
    base = _seeded_resolver()
    share_kinds = {
        "web": ServiceShareKind.SYSTEM,
        "terminal": ServiceShareKind.APP,
        "bad_name": ServiceShareKind.INTERNAL,
    }
    resolver = base.model_copy_update(to_update(base.field_ref().share_kinds_by_agent_id, {_AGENT_ID: share_kinds}))
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/options")

    data = json.loads(response.get_data(as_text=True))
    assert data["app_services"] == ["terminal"]
    assert data["system_services"] == ["web"]
    assert data["service_labels"] == {"web": "web-r4nd", "system_interface": "shell-r4nd"}


def test_accepted_service_icon_refuses_anything_executable() -> None:
    good = '<svg viewBox="0 0 24 24"><path d="M2 2h20"/></svg>'
    assert accepted_service_icon(good) == good
    assert accepted_service_icon("  " + good + "  ") == good
    assert accepted_service_icon("") == ""
    assert accepted_service_icon("not an svg") == ""
    assert accepted_service_icon('<div><svg viewBox="0 0 24 24"></svg></div>') == ""
    assert accepted_service_icon('<svg onload="x()"></svg>') == ""
    assert accepted_service_icon("<svg><script>1</script></svg>") == ""
    assert accepted_service_icon('<svg><a href="javascript:alert(1)">x</a></svg>') == ""
    assert accepted_service_icon("<svg><style>svg{}</style></svg>") == ""
    assert accepted_service_icon("<svg><foreignObject/></svg>") == ""
    assert accepted_service_icon("<svg>" + "a" * 16400 + "</svg>") == ""


def test_workspace_host_coordinate_prefers_discovery() -> None:
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={},
        display_info_by_agent_id={_AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id=_HOST_ID)},
    )
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == _HOST_ID


def test_workspace_host_coordinate_empty_for_undiscovered_agent_without_records() -> None:
    resolver = _OptionsSeededResolver(url_by_agent_and_service={})
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == ""


def test_workspace_host_coordinate_ignores_non_host_shaped_coordinates() -> None:
    # A local workspace's coordinate is not a host-<hex> id; the sharing API
    # has nothing to key by, so the pane must get '' rather than 'localhost'.
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={},
        display_info_by_agent_id={_AGENT_ID: AgentDisplayInfo(agent_name="sunny", host_id="localhost")},
    )
    assert _workspace_host_coordinate_for_options(resolver, None, _AGENT_ID) == ""


def test_machine_size_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size")

    assert response.status_code == 401


def test_machine_size_rejects_malformed_agent_ids(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get("/ui/api/workspaces/not-an-agent-id/machine-size")

    assert response.status_code == 404


@pytest.mark.witnesses(
    "machine-size.unknown-size-named", partial="the page's rendering is covered by the frontend tests"
)
def test_machine_size_is_unavailable_when_discovery_recorded_no_size(tmp_path: Path) -> None:
    # The seeded resolver carries no size for the workspace's host, so the
    # section says the machine does not report one.
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=_seeded_resolver()
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size")

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["is_available"] is False
    assert body["is_read_failed"] is False
    assert body["memory_gb"] is None


@pytest.mark.witnesses("machine-size.recorded-size-shown")
@pytest.mark.parametrize(
    ("provider_name", "is_resizable", "is_disk_resizable"),
    [("docker", True, False), ("lima", True, True), ("modal", False, False), ("aws_" + "0" * 32, False, False)],
)
def test_machine_size_reports_the_size_discovery_recorded_for_any_provider(
    tmp_path: Path, provider_name: str, is_resizable: bool, is_disk_resizable: bool
) -> None:
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={_AGENT_ID: {}},
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="sized", host_id=_HOST_ID, provider_name=provider_name)
        },
        host_size_by_host_id={_HOST_ID: DiscoveredHostSize(cpu_count=4, memory_gb=8.0, disk_gb=100.0)},
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=resolver
    )

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size")

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["is_available"] is True
    assert body["cpu_count"] == 4
    assert body["memory_gb"] == 8.0
    assert body["disk_gb"] == 100.0
    assert body["is_resizable"] is is_resizable
    assert body["is_disk_resizable"] is is_disk_resizable
    assert body["is_restart_needed_to_apply"] is False
    # A provider's offer is known from the provider alone: nothing is left to read.
    assert body["is_resize_offer_pending"] is False
    assert body["is_restart_in_progress"] is False


class _MachineSizeStubCli(FakeImbueCloudCli):
    """Fake whose ``show_machine`` returns a canned size and records its arguments.

    The real ``show_machine`` subprocess parsing is covered by
    ``imbue_cloud_cli_test.py``; this stub isolates the endpoint's glue (which
    coordinates it passes, how the result is mapped onto the wire model).
    """

    machine_to_return: MachineSizeCliInfo | None = Field(default=None)
    show_machine_calls: list[tuple[str, str]] = Field(default_factory=list)
    account_info_call_count: int = Field(default=0, description="How many times get_account_info was called")

    def show_machine(self, account: str, machine_ref: str) -> MachineSizeCliInfo | None:
        self.show_machine_calls.append((account, machine_ref))
        return self.machine_to_return

    def get_account_info(self, account: str) -> dict[str, Any]:
        self.account_info_call_count += 1
        return super().get_account_info(account)


_OWNER_USER_ID = "33333333-3333-3333-3333-333333333333"


def _ally_account_info(active_machine_units: int, total_machine_disk_gb: int, plan_name: str = "ally") -> dict:
    """The `account show` document of an account on ``plan_name`` with the ally quotas and the given usage."""
    return {
        "user_id": _OWNER_USER_ID,
        "email": "owner@example.com",
        "plan_name": plan_name,
        "entitlements": {
            "max_remote_workspaces": 10,
            "max_total_workspaces": 50,
            "max_buckets": 10,
            "max_total_bucket_bytes": 1,
            "monthly_llm_spend_usd": 1.0,
            "max_active_synced_workspaces": 10,
            "max_active_machine_units": 80,
            "max_total_machine_disk_gb": 1400,
        },
        "usage": {
            "remote_workspaces": 2,
            "total_workspaces": 2,
            "buckets": 0,
            "total_bucket_bytes": 0,
            "llm_spend_usd_this_period": 0.0,
            "active_synced_workspaces": 2,
            "active_machine_units": active_machine_units,
            "total_machine_disk_gb": total_machine_disk_gb,
        },
        "available_plans": ["free", "explorer", "ally"],
    }


def _leased_machine_size_client(tmp_path: Path, cli: _MachineSizeStubCli) -> FlaskClient:
    """A client whose one workspace is a leased Imbue Cloud machine linked to ``owner@example.com``."""
    resolver = _OptionsSeededResolver(
        url_by_agent_and_service={_AGENT_ID: {}},
        display_info_by_agent_id={
            _AGENT_ID: AgentDisplayInfo(agent_name="leased", host_id=_HOST_ID, provider_name="imbue_cloud_alice")
        },
    )
    cli.add_account(user_id=_OWNER_USER_ID, email="owner@example.com")
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=cli)
    session_store.associate_created_workspace(
        user_id=_OWNER_USER_ID, agent_id=_AGENT_ID, host_id=_HOST_ID, display_name="", color=None, is_cloud_row=False
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        imbue_cloud_cli=cli,
        session_store=session_store,
    )
    return client


@pytest.mark.witnesses(
    "machine-size.imbue-cloud-size-and-pending-change", partial="the page's rendering is covered by the frontend tests"
)
def test_machine_size_reports_the_leased_machines_sizes(tmp_path: Path) -> None:
    cli = _MachineSizeStubCli(
        connector_url=FAKE_CONNECTOR_URL,
        machine_to_return=MachineSizeCliInfo(
            host_db_id="row-1",
            host_id=_HOST_ID,
            host_name="sunny",
            status="running",
            memory_units=8,
            target_memory_units=16,
            disk_gb=28,
            target_disk_gb=None,
            is_restart_needed_to_apply=True,
        ),
    )
    client = _leased_machine_size_client(tmp_path, cli)

    response = client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size")

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["is_available"] is True
    # One unit is one GiB; the bake-time vCPU count is not shown since it does not follow a resize.
    assert body["cpu_count"] is None
    assert body["memory_gb"] == 8
    assert body["target_memory_gb"] == 16
    assert body["disk_gb"] == 28
    assert body["target_disk_gb"] is None
    assert body["is_resizable"] is False
    assert body["is_restart_needed_to_apply"] is True
    # The lookup keys the connector by the associated account and the
    # machine's host coordinate (not the agent id).
    assert cli.show_machine_calls == [("owner@example.com", _HOST_ID)]
    # Whether the size may be changed waits on the slower account read, which
    # a plain read never makes: the page asks for it separately.
    assert body["is_resize_offer_pending"] is True
    assert cli.account_info_call_count == 0


@pytest.mark.witnesses(
    "machine-size.unread-size-named", partial="the page's rendering is covered by the frontend tests"
)
def test_machine_size_says_a_leased_machines_size_could_not_be_read_when_the_connector_fails(tmp_path: Path) -> None:
    # The stub answers `machines show` with nothing, as the CLI does when the
    # connector fails: a leased machine always has a size, so the page is told
    # the size could not be read, not that the machine reports none.
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=None)
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size").data)

    assert body["is_available"] is False
    assert body["is_read_failed"] is True
    assert body["memory_gb"] is None
    assert cli.show_machine_calls == [("owner@example.com", _HOST_ID)]


_RUNNING_DEFAULT_MACHINE = MachineSizeCliInfo(
    host_db_id="row-1",
    host_id=_HOST_ID,
    host_name="sunny",
    status="running",
    memory_units=8,
    target_memory_units=None,
    disk_gb=28,
    target_disk_gb=None,
    is_restart_needed_to_apply=False,
)


@pytest.mark.witnesses(
    "machine-size.imbue-cloud-sizes-offered-within-the-plan",
    partial="the page's rendering is covered by the frontend tests",
)
def test_machine_size_offers_an_ally_account_the_sizes_its_quotas_would_grant(tmp_path: Path) -> None:
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    # This machine's 8 units plus another running machine's 8; 28 GB of disk each.
    cli.account_info_by_email["owner@example.com"] = _ally_account_info(
        active_machine_units=16, total_machine_disk_gb=56
    )
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)

    assert body["is_resizable"] is True
    assert body["is_resize_offer_pending"] is False
    assert body["is_disk_resizable"] is True
    assert body["is_running"] is True
    assert body["is_transitioning"] is False
    assert body["memory_choices_gb"] == [8, 16, 24, 32, 40, 48, 56, 64, 72]
    assert body["is_memory_capped_by_plan"] is True
    assert body["plan_memory_limit_gb"] == 80
    assert body["other_machines_memory_gb"] == 8
    assert body["max_disk_gb"] == 1372
    assert body["plan_disk_limit_gb"] == 1400
    assert body["other_machines_disk_gb"] == 28


@pytest.mark.witnesses(
    "machine-size.imbue-cloud-sizes-offered-to-the-ally-plan-only",
    partial="the page's rendering is covered by the frontend tests",
)
def test_machine_size_stays_read_only_for_an_account_off_the_ally_plan(tmp_path: Path) -> None:
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    cli.account_info_by_email["owner@example.com"] = _ally_account_info(
        active_machine_units=8, total_machine_disk_gb=28, plan_name="explorer"
    )
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)

    assert body["is_available"] is True
    assert body["memory_gb"] == 8
    assert body["is_resizable"] is False
    # The offer was read and is not pending any more: the page shows the size read-only.
    assert body["is_resize_offer_pending"] is False
    assert body["memory_choices_gb"] == []
    assert body["max_disk_gb"] is None


def test_machine_size_stays_read_only_when_the_accounts_plan_cannot_be_read(tmp_path: Path) -> None:
    """A connector that answers the machine but not the account still shows the size, read-only."""
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)

    assert body["is_available"] is True
    assert body["memory_gb"] == 8
    assert body["is_resizable"] is False


def test_machine_size_stays_read_only_against_a_connector_without_machine_quotas(tmp_path: Path) -> None:
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    info = _ally_account_info(active_machine_units=8, total_machine_disk_gb=28)
    del info["entitlements"]["max_active_machine_units"]
    del info["usage"]["active_machine_units"]
    cli.account_info_by_email["owner@example.com"] = info
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)

    assert body["is_available"] is True
    assert body["is_resizable"] is False


@pytest.mark.witnesses(
    "machine-size.no-resize-while-the-machine-transitions",
    partial="the page's rendering is covered by the frontend tests",
)
def test_machine_size_reports_a_starting_machine_as_transitioning(tmp_path: Path) -> None:
    starting = _RUNNING_DEFAULT_MACHINE.model_copy_update(
        to_update(_RUNNING_DEFAULT_MACHINE.field_ref().status, "starting")
    )
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=starting)
    cli.account_info_by_email["owner@example.com"] = _ally_account_info(
        active_machine_units=8, total_machine_disk_gb=28
    )
    client = _leased_machine_size_client(tmp_path, cli)

    body = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)

    assert body["is_resizable"] is True
    assert body["is_running"] is False
    assert body["is_transitioning"] is True


def test_machine_size_reads_the_offer_only_when_asked_for_it(tmp_path: Path) -> None:
    """The size answers from one connector read; the offer costs the account read and is asked for separately."""
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    cli.account_info_by_email["owner@example.com"] = _ally_account_info(
        active_machine_units=8, total_machine_disk_gb=28
    )
    client = _leased_machine_size_client(tmp_path, cli)

    size_only = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size").data)
    assert size_only["memory_gb"] == 8
    assert size_only["is_resize_offer_pending"] is True
    assert size_only["is_resizable"] is False
    assert cli.account_info_call_count == 0

    offered = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size?include_resize_offer=1").data)
    assert offered["is_resize_offer_pending"] is False
    assert offered["is_resizable"] is True
    assert cli.account_info_call_count == 1


def test_machine_size_reports_the_restart_this_app_is_running(tmp_path: Path) -> None:
    """A host recovery under way is reported so the page offers no second restart until it ends."""
    cli = _MachineSizeStubCli(connector_url=FAKE_CONNECTOR_URL, machine_to_return=_RUNNING_DEFAULT_MACHINE)
    client = _leased_machine_size_client(tmp_path, cli)
    registry = get_state(client.application).workspace_operation_registry

    before = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size").data)
    assert before["is_restart_in_progress"] is False

    registry.start(AgentId(_AGENT_ID), WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    during = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size").data)
    assert during["is_restart_in_progress"] is True

    registry.complete(AgentId(_AGENT_ID))
    after = json.loads(client.get(f"/ui/api/workspaces/{_AGENT_ID}/machine-size").data)
    assert after["is_restart_in_progress"] is False

    # A size that cannot be shown still answers it: the page follows the
    # answer to know when a restart it dispatched is over.
    unknown_agent_id = "agent-" + "b" * 32
    registry.start(AgentId(unknown_agent_id), WorkspaceOperationKind.RECOVERY, datetime.now(timezone.utc))
    unavailable = json.loads(client.get(f"/ui/api/workspaces/{unknown_agent_id}/machine-size").data)
    assert unavailable["is_available"] is False
    assert unavailable["is_restart_in_progress"] is True
