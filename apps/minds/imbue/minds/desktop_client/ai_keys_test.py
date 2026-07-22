"""Tests for the workspace AI-key mint helpers and routes (see ai_keys.py)."""

from pathlib import Path

from flask.testing import FlaskClient
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.ai_keys import build_credential_blob
from imbue.minds.desktop_client.ai_keys import mint_workspace_credential_blob
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import RecordingImbueCloudCli
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore


class _FixedEmailSessionStore(MultiAccountSessionStore):
    """Session store double answering ``get_account_email`` from a fixed map."""

    email_by_user_id: dict[str, str] = Field(default_factory=dict)

    def get_account_email(self, user_id: str) -> str | None:
        return self.email_by_user_id.get(user_id)


def _make_record_store(tmp_path: Path) -> WorkspaceRecordStore:
    # cli=None keeps every mutation local (no push subprocess).
    return WorkspaceRecordStore(
        paths=WorkspacePaths(data_dir=tmp_path),
        cli=None,
        device_id="device-test",
        device_label="test-device",
    )


def _make_session_store(tmp_path: Path, email_by_user_id: dict[str, str]) -> _FixedEmailSessionStore:
    return _FixedEmailSessionStore(
        data_dir=tmp_path,
        cli=FakeImbueCloudCli(connector_url=FAKE_CONNECTOR_URL),
        record_store=_make_record_store(tmp_path / "session-records"),
        email_by_user_id=email_by_user_id,
    )


def _active_record(host_id: str, agent_id: str, display_name: str = "") -> ReplicaRecord:
    return ReplicaRecord(
        host_id=host_id,
        agent_id=agent_id,
        display_name=display_name,
        state=RECORD_STATE_ACTIVE,
    )


def test_resolve_workspace_account_finds_owning_account(tmp_path: Path) -> None:
    record_store = _make_record_store(tmp_path)
    record_store.upsert_local_record(
        "user-1", "alice@example.com", _active_record("host-abc", "agent-1", display_name="my-ws")
    )
    session_store = _make_session_store(tmp_path, {"user-1": "alice@example.com"})

    resolved = resolve_workspace_account("host-abc", record_store, session_store)

    assert resolved is not None
    assert resolved.user_id == "user-1"
    assert resolved.account_email == "alice@example.com"
    assert resolved.workspace_display_name == "my-ws"


def test_resolve_workspace_account_returns_none_for_unassociated_host(tmp_path: Path) -> None:
    record_store = _make_record_store(tmp_path)
    session_store = _make_session_store(tmp_path, {"user-1": "alice@example.com"})

    assert resolve_workspace_account("host-unknown", record_store, session_store) is None


def test_resolve_workspace_account_ignores_destroyed_records(tmp_path: Path) -> None:
    record_store = _make_record_store(tmp_path)
    active = _active_record("host-abc", "agent-1")
    destroyed = active.model_copy_update(to_update(active.field_ref().state, "destroyed"))
    record_store.upsert_local_record("user-1", "alice@example.com", destroyed)
    session_store = _make_session_store(tmp_path, {"user-1": "alice@example.com"})

    assert resolve_workspace_account("host-abc", record_store, session_store) is None


def test_resolve_workspace_account_tolerates_missing_stores(tmp_path: Path) -> None:
    session_store = _make_session_store(tmp_path, {})
    assert resolve_workspace_account("host-abc", None, None) is None
    assert resolve_workspace_account("host-abc", _make_record_store(tmp_path / "r"), None) is None
    assert resolve_workspace_account("host-abc", None, session_store) is None


def test_build_credential_blob_is_env_var_lines() -> None:
    blob = build_credential_blob(api_key="sk-x", base_url="https://litellm.example.com/")
    assert blob == "ANTHROPIC_BASE_URL=https://litellm.example.com/\nANTHROPIC_API_KEY=sk-x\n"


def test_mint_workspace_credential_blob_fixes_workspace_identity_on_the_key(tmp_path: Path) -> None:
    """The key's alias/metadata carry the workspace host id server-side; there is
    no user-editable naming input by design."""
    cli = RecordingImbueCloudCli(connector_url=FAKE_CONNECTOR_URL)

    blob = mint_workspace_credential_blob(
        workspace_host_id="host-abc", account_email="alice@example.com", imbue_cloud_cli=cli
    )

    assert len(cli.create_calls) == 1
    call = cli.create_calls[0]
    assert call["account"] == "alice@example.com"
    assert call["alias"] == "workspace-host-abc"
    assert call["max_budget"] == 100.0
    assert call["budget_duration"] == "1d"
    assert call["metadata"] == {"workspace_host_id": "host-abc", "source": "ai-keys-page"}
    assert "ANTHROPIC_API_KEY=sk-fake-litellm-key" in blob
    assert "ANTHROPIC_BASE_URL=https://litellm.example.com/" in blob


# ---------------------------------------------------------------------------
# Route tests: GET /settings/ai-keys and POST /settings/ai-keys/mint
# ---------------------------------------------------------------------------


class _FailingMintImbueCloudCli(FakeImbueCloudCli):
    """CLI double whose ``create_litellm_key`` always fails (connector down, etc.)."""

    def create_litellm_key(
        self,
        *,
        account: str,
        alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> LiteLLMKeyMaterial:
        raise ImbueCloudCliError("keys litellm create: connector unreachable (fake)")


def _build_ai_keys_client(
    tmp_path: Path,
    record_store: WorkspaceRecordStore | None = None,
    session_store: MultiAccountSessionStore | None = None,
    imbue_cloud_cli: ImbueCloudCli | None = None,
    is_authenticated: bool = True,
) -> FlaskClient:
    """Build a test client for the ai-keys routes.

    The sync scheduler (the route's source for the workspace-record store) is
    constructed but never started, mirroring sync_scheduler_test.py.
    """
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    sync_scheduler = None
    if record_store is not None and session_store is not None:
        sync_scheduler = WorkspaceSyncScheduler(
            record_store=record_store,
            session_store=session_store,
            resolver=StaticBackendResolver(url_by_agent_and_service={}),
        )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        imbue_cloud_cli=imbue_cloud_cli,
        session_store=session_store,
        sync_scheduler=sync_scheduler,
    )
    client = app.test_client()
    if is_authenticated:
        client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _build_associated_workspace_client(tmp_path: Path, imbue_cloud_cli: ImbueCloudCli | None) -> FlaskClient:
    """Client whose record store associates host-abc with alice's account."""
    record_store = _make_record_store(tmp_path / "records")
    record_store.upsert_local_record(
        "user-1", "alice@example.com", _active_record("host-abc", "agent-1", display_name="my-ws")
    )
    session_store = _make_session_store(tmp_path / "session", {"user-1": "alice@example.com"})
    return _build_ai_keys_client(
        tmp_path, record_store=record_store, session_store=session_store, imbue_cloud_cli=imbue_cloud_cli
    )


def test_ai_keys_routes_require_authentication(tmp_path: Path) -> None:
    client = _build_ai_keys_client(tmp_path, is_authenticated=False)

    assert client.get("/settings/ai-keys?workspace=host-abc").status_code == 403
    assert client.post("/settings/ai-keys/mint", json={"workspace": "host-abc"}).status_code == 403


def test_ai_keys_page_without_workspace_explains_how_to_get_there(tmp_path: Path) -> None:
    client = _build_ai_keys_client(tmp_path)

    response = client.get("/settings/ai-keys")

    assert response.status_code == 200
    assert "opened from a workspace" in response.text
    assert 'id="mint-key"' not in response.text


def test_ai_keys_page_errors_for_unassociated_workspace(tmp_path: Path) -> None:
    client = _build_ai_keys_client(tmp_path)

    response = client.get("/settings/ai-keys?workspace=host-unknown")

    assert response.status_code == 200
    assert "no associated Imbue account" in response.text
    assert 'id="mint-key"' not in response.text


def test_ai_keys_page_shows_workspace_and_billed_account(tmp_path: Path) -> None:
    client = _build_associated_workspace_client(tmp_path, RecordingImbueCloudCli(connector_url=FAKE_CONNECTOR_URL))

    response = client.get("/settings/ai-keys?workspace=host-abc")

    assert response.status_code == 200
    assert "my-ws" in response.text
    assert "alice@example.com" in response.text
    assert 'id="mint-key"' in response.text


def test_mint_requires_workspace_field(tmp_path: Path) -> None:
    client = _build_ai_keys_client(tmp_path)

    response = client.post("/settings/ai-keys/mint", json={})

    assert response.status_code == 400
    assert "workspace" in response.get_json()["error"]


def test_mint_rejects_unassociated_workspace(tmp_path: Path) -> None:
    client = _build_ai_keys_client(tmp_path)

    response = client.post("/settings/ai-keys/mint", json={"workspace": "host-unknown"})

    assert response.status_code == 400
    assert "no associated Imbue account" in response.get_json()["error"]


def test_mint_without_imbue_cloud_cli_returns_501(tmp_path: Path) -> None:
    client = _build_associated_workspace_client(tmp_path, imbue_cloud_cli=None)

    response = client.post("/settings/ai-keys/mint", json={"workspace": "host-abc"})

    assert response.status_code == 501


def test_mint_returns_credential_blob_for_associated_workspace(tmp_path: Path) -> None:
    cli = RecordingImbueCloudCli(connector_url=FAKE_CONNECTOR_URL)
    client = _build_associated_workspace_client(tmp_path, cli)

    response = client.post("/settings/ai-keys/mint", json={"workspace": "host-abc"})

    assert response.status_code == 200
    credentials = response.get_json()["credentials"]
    assert "ANTHROPIC_API_KEY=sk-fake-litellm-key" in credentials
    assert "ANTHROPIC_BASE_URL=https://litellm.example.com/" in credentials
    assert len(cli.create_calls) == 1
    assert cli.create_calls[0]["alias"] == "workspace-host-abc"


def test_mint_maps_cli_failure_to_502(tmp_path: Path) -> None:
    client = _build_associated_workspace_client(tmp_path, _FailingMintImbueCloudCli(connector_url=FAKE_CONNECTOR_URL))

    response = client.post("/settings/ai-keys/mint", json={"workspace": "host-abc"})

    assert response.status_code == 502
    assert "Failed to create the key" in response.get_json()["error"]
