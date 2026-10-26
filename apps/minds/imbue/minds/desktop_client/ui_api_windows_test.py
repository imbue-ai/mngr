"""Tests for the /ui/api/windows routes: where a switching window lands, and whose account a route is."""

import json
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import write_dead_destroy_marker
from imbue.minds.desktop_client.ui_models import UiClientStateMessage
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def _switch(
    client: FlaskClient, account_id: str, client_id: str = "win-asking", is_desktop: bool = True
) -> tuple[int, dict[str, object]]:
    response = client.post(
        "/ui/api/windows/switch-account",
        data=json.dumps({"account_id": account_id, "client_id": client_id, "is_desktop": is_desktop}),
        content_type="application/json",
    )
    return response.status_code, json.loads(response.get_data(as_text=True))


@pytest.mark.witnesses("account-windows.switch-lands-on-a-free-recent-workspace")
@pytest.mark.witnesses("account-windows.switch-skips-a-workspace-another-window-shows")
@pytest.mark.witnesses("account-windows.switch-falls-back-to-the-home-page")
def test_switch_account_lands_on_the_accounts_most_recent_workspace_no_other_window_shows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-alice", email="alice@example.com")
    recent_agent = AgentId.generate()
    older_agent = AgentId.generate()
    # Only the recent workspace is still listed; the older one is gone from the machine.
    resolver = build_resolver_with_system_services(
        recent_agent,
        AgentId.generate(),
        workspace_certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    minds_config = MindsConfig(data_dir=tmp_path / "minds-config")
    paths = InstallationPaths(data_dir=tmp_path)
    client, app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        imbue_cloud_cli=cli,
        session_store=session_store,
        minds_config=minds_config,
        paths=paths,
    )
    for agent_id in (recent_agent, older_agent):
        session_store.associate_created_workspace(
            user_id="user-alice",
            agent_id=str(agent_id),
            host_id=f"host-{agent_id.get_uuid().hex}",
            display_name="ws",
            color=None,
            is_cloud_row=False,
        )
    # Alice's windows opened the older workspace, then the recent one.
    minds_config.note_window_shows("user-alice", str(older_agent))
    minds_config.note_window_shows("user-alice", str(recent_agent))

    status, body = _switch(client, "user-alice")
    assert (status, body) == (200, {"route": f"/workspace/{recent_agent}"})

    # Another desktop window shows it: the switch lands on the hub, since the older one is gone.
    broadcaster = get_state(app).ui_channel_broadcaster
    other_queue = broadcaster.register()
    broadcaster.set_client_state(
        other_queue,
        UiClientStateMessage(
            client_id="win-other", route="/workspace/x", workspace_agent_id=str(recent_agent), account_id="user-alice"
        ),
    )
    status, body = _switch(client, "user-alice")
    assert (status, body) == (200, {"route": "/account/user-alice/"})
    # Browser tabs are not deduplicated, so a tab asking still lands on it.
    status, body = _switch(client, "user-alice", is_desktop=False)
    assert (status, body) == (200, {"route": f"/workspace/{recent_agent}"})

    # The asking window's own workspace does not hold it back.
    status, body = _switch(client, "user-alice", client_id="win-other")
    assert (status, body) == (200, {"route": f"/workspace/{recent_agent}"})

    # A workspace whose destroy failed is still listed, but cannot be entered.
    write_dead_destroy_marker(paths, recent_agent, HostId.generate(), exit_code=137)
    status, body = _switch(client, "user-alice", client_id="win-other")
    assert (status, body) == (200, {"route": "/account/user-alice/"})


@pytest.mark.witnesses("account-windows.switch-falls-back-to-the-home-page")
def test_switch_account_lands_on_the_hub_when_the_account_has_no_recent_workspace(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
    )

    assert _switch(client, "local") == (200, {"route": "/account/local/"})
    assert _switch(client, "user-nobody") == (200, {"route": "/account/user-nobody/"})


def test_switch_account_refuses_a_bad_body_and_an_unauthenticated_caller(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
    )
    assert _switch(client, "../etc")[0] == 400
    assert client.post("/ui/api/windows/switch-account", data="{}", content_type="application/json").status_code == 400

    signed_out, _app, _auth_store = build_desktop_client_for_test(
        tmp_path / "other", is_authenticated=False, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
    )
    assert _switch(signed_out, "local")[0] == 401


def _route_account(client: FlaskClient, path: str, query: str = "") -> tuple[int, dict[str, object]]:
    response = client.get("/ui/api/windows/route-account", query_string={"path": path, "query": query})
    return response.status_code, json.loads(response.get_data(as_text=True))


def test_route_account_names_a_workspace_routes_owner_from_the_records_by_either_coordinate(tmp_path: Path) -> None:
    """A window stepping back onto a workspace of the account it left asks whose it is; the records say, discovery or not."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-alice", email="alice@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        imbue_cloud_cli=cli,
        session_store=session_store,
    )
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    session_store.associate_created_workspace(
        user_id="user-alice",
        agent_id=str(agent_id),
        host_id=str(host_id),
        display_name="ws",
        color=None,
        is_cloud_row=True,
    )

    assert _route_account(client, f"/workspace/{agent_id}") == (200, {"account_id": "user-alice"})
    assert _route_account(client, f"/workspace/{host_id}/backups") == (200, {"account_id": "user-alice"})
    # A workspace no record claims is the device's, and a hub route names its account outright.
    assert _route_account(client, f"/workspace/{AgentId.generate()}") == (200, {"account_id": "local"})
    assert _route_account(client, "/account/user-bob/create") == (200, {"account_id": "user-bob"})


@pytest.mark.witnesses(
    "home-page.bare-root-redirects",
    partial="witnesses the account the server resolves a bare route to; the SPA's redirect to that "
    "account's home page is a frontend concern",
)
def test_bare_route_takes_the_signed_in_account_with_the_most_workspaces_until_one_is_recorded(
    tmp_path: Path,
) -> None:
    """An install upgraded from before the recent list has none recorded; a signed-in account with workspaces wins over the device."""
    cli = make_fake_imbue_cloud_cli()
    for user_id in ("user-alice", "user-bob", "user-carol"):
        cli.add_account(user_id=user_id, email=f"{user_id}@example.com")
    session_store = make_session_store_for_test(tmp_path / "session-store", cli)
    minds_config = MindsConfig(data_dir=tmp_path / "minds-data")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        imbue_cloud_cli=cli,
        session_store=session_store,
        minds_config=minds_config,
    )
    assert _route_account(client, "/") == (200, {"account_id": "local"})

    for user_id, workspace_count in (("user-alice", 1), ("user-bob", 2)):
        for _ in range(workspace_count):
            session_store.associate_created_workspace(
                user_id=user_id,
                agent_id=str(AgentId.generate()),
                host_id=str(HostId.generate()),
                display_name="ws",
                color=None,
                is_cloud_row=True,
            )

    assert _route_account(client, "/") == (200, {"account_id": "user-bob"})
    # An account signed out here does not stand in, however many workspaces its records name.
    record_store = session_store.record_store
    assert record_store is not None
    for _ in range(3):
        record_store.upsert_local_record(
            "user-gone",
            "gone@example.com",
            ReplicaRecord(host_id=str(HostId.generate()), agent_id=str(AgentId.generate()), display_name="ws"),
        )
    assert _route_account(client, "/") == (200, {"account_id": "user-bob"})
    # Once a window has shown an account, the recent list decides.
    minds_config.note_window_shows("user-alice", None)
    assert _route_account(client, "/") == (200, {"account_id": "user-alice"})


def test_route_account_refuses_a_bad_path_and_an_unauthenticated_caller(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
    )
    assert _route_account(client, "workspace/agent-0a1b")[0] == 400
    assert _route_account(client, "")[0] == 400

    signed_out, _app, _auth_store = build_desktop_client_for_test(
        tmp_path / "other", is_authenticated=False, backend_resolver=StaticBackendResolver(url_by_agent_and_service={})
    )
    assert _route_account(signed_out, "/workspace/agent-0a1b")[0] == 401
