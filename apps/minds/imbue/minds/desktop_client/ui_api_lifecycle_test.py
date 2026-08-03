import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from flask.testing import FlaskClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId

_DESTROYED_AGENT_ID = "agent-" + "9" * 32
_TEST_USER_ID = "user-test-123"
_TEST_EMAIL = "test@example.com"


def _seed_destroyed_record(tmp_path: Path, cli: FakeImbueCloudCli, destroyed_days_ago: int = 3) -> None:
    seed_store = make_session_store_for_test(tmp_path, cli=cli)
    assert seed_store.record_store is not None
    destroyed_at = (datetime.now(timezone.utc) - timedelta(days=destroyed_days_ago)).isoformat()
    seed_store.record_store.upsert_local_record(
        _TEST_USER_ID,
        _TEST_EMAIL,
        ReplicaRecord(
            host_id="host-destroyed1",
            agent_id=_DESTROYED_AGENT_ID,
            display_name="old-workspace",
            state="destroyed",
            destroyed_at=destroyed_at,
        ),
    )


def _build_lifecycle_client(
    tmp_path: Path,
    cli: FakeImbueCloudCli | None = None,
    is_authenticated: bool = True,
    is_reaper_wired: bool = False,
) -> tuple[FlaskClient, MultiAccountSessionStore]:
    """A desktop-client test app with the stores the lifecycle routes read."""
    effective_cli = cli if cli is not None else make_fake_imbue_cloud_cli()
    session_store = make_session_store_for_test(tmp_path, cli=effective_cli)
    sync_scheduler = None
    if is_reaper_wired:
        record_store = session_store.record_store
        assert record_store is not None
        reaper = BackupReaperManager(
            paths=record_store.paths,
            record_store=record_store,
            imbue_cloud_cli=None,
            connector_url="",
        )
        sync_scheduler = WorkspaceSyncScheduler(
            record_store=record_store,
            session_store=session_store,
            resolver=StaticBackendResolver(url_by_agent_and_service={}),
            backup_reaper=reaper,
        )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        paths=WorkspacePaths(data_dir=tmp_path),
        session_store=session_store,
        sync_scheduler=sync_scheduler,
    )
    return client, session_store


def test_destroyed_workspaces_endpoint_requires_a_session(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 401
    assert json.loads(response.data)["error"] == "Not authenticated"


def test_destroyed_workspaces_endpoint_lists_tombstoned_records_as_typed_rows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["retention_days"] >= 1
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["agent_id"] == _DESTROYED_AGENT_ID
    assert row["display_name"] == "old-workspace"
    assert row["account_label"] == _TEST_EMAIL
    assert "until deletion" in row["days_left_display"]
    assert row["can_delete"] is True
    # No local env and no synced secrets: nothing to download, nothing locked.
    assert row["has_backup"] is False
    assert row["can_download"] is False
    assert row["is_locked"] is False


def test_destroyed_workspaces_endpoint_returns_empty_rows_when_nothing_is_destroyed(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 200
    assert json.loads(response.data)["rows"] == []


def test_delete_destroyed_backup_reaps_the_record(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, session_store = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post(f"/ui/api/destroyed-workspaces/{_DESTROYED_AGENT_ID}/delete-backup")

    assert response.status_code == 200
    assert json.loads(response.data) == {"is_deleted": True}
    assert session_store.record_store is not None
    assert session_store.record_store.list_records(_TEST_USER_ID) == []


def test_delete_destroyed_backup_rejects_an_unknown_agent(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post("/ui/api/destroyed-workspaces/agent-doesnotexist/delete-backup")

    assert response.status_code == 404
    assert "No destroyed machine found" in json.loads(response.data)["error"]


def test_delete_destroyed_backup_rejects_a_well_formed_but_unknown_agent(tmp_path: Path) -> None:
    """A parseable AgentId with no tombstoned record and no orphan env must 404
    rather than reporting a successful deletion of nothing."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post(f"/ui/api/destroyed-workspaces/{AgentId()}/delete-backup")

    assert response.status_code == 404
    assert "No destroyed machine found" in json.loads(response.data)["error"]


def test_delete_destroyed_backup_without_a_reaper_is_a_conflict(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=False)

    response = client.post(f"/ui/api/destroyed-workspaces/{_DESTROYED_AGENT_ID}/delete-backup")

    assert response.status_code == 409
    assert "not configured" in json.loads(response.data)["error"]


def test_recovery_info_requires_a_session(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path, is_authenticated=False)

    response = client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info")

    assert response.status_code == 401


def test_recovery_info_resolves_an_agent_keyed_workspace_with_healthy_defaults(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path)

    response = client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["agent_id"] == _DESTROYED_AGENT_ID
    # No tracker wired in this minimal app: health defaults to healthy and
    # the name falls back to the agent id.
    assert payload["health"] == "healthy"
    assert payload["workspace_name"] == _DESTROYED_AGENT_ID
    assert payload["ssh_command"] == ""
    assert payload["is_host_offline"] is False


def test_recovery_info_resolves_a_host_keyed_coordinate_through_workspace_records(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/workspaces/host-destroyed1/recovery-info")

    assert response.status_code == 200
    assert json.loads(response.data)["agent_id"] == _DESTROYED_AGENT_ID


def test_recovery_info_rejects_an_unknown_coordinate(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path)

    response = client.get("/ui/api/workspaces/host-00000000000000000000000000000abc/recovery-info")

    assert response.status_code == 404
