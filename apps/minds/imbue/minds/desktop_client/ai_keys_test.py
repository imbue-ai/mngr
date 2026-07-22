"""Tests for the workspace AI-key mint helpers (see ai_keys.py)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.model_update import to_update
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.ai_keys import build_credential_blob
from imbue.minds.desktop_client.ai_keys import mint_workspace_credential_blob
from imbue.minds.desktop_client.ai_keys import resolve_workspace_account
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import LiteLLMKeyMaterial
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore


class _FixedEmailSessionStore(MultiAccountSessionStore):
    """Session store double answering ``get_account_email`` from a fixed map."""

    email_by_user_id: dict[str, str] = Field(default_factory=dict)

    def get_account_email(self, user_id: str) -> str | None:
        return self.email_by_user_id.get(user_id)


class _MintRecordingImbueCloudCli(FakeImbueCloudCli):
    """Records ``create_litellm_key`` calls and returns stub key material."""

    create_calls: list[dict[str, object]] = Field(default_factory=list)

    def create_litellm_key(
        self,
        *,
        account: str,
        alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> LiteLLMKeyMaterial:
        self.create_calls.append(
            {
                "account": account,
                "alias": alias,
                "max_budget": max_budget,
                "budget_duration": budget_duration,
                "metadata": dict(metadata) if metadata is not None else None,
            }
        )
        return LiteLLMKeyMaterial(
            key=SecretStr("sk-fake-minted-key"),
            base_url=AnyUrl("https://litellm.example.com/"),
        )


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
    cli = _MintRecordingImbueCloudCli(connector_url=FAKE_CONNECTOR_URL)

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
    assert "ANTHROPIC_API_KEY=sk-fake-minted-key" in blob
    assert "ANTHROPIC_BASE_URL=https://litellm.example.com/" in blob
