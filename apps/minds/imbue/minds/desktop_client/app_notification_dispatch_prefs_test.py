"""Unit tests for the OS-dispatch preferences reader: each entry's master toggle is its own account's."""

from pathlib import Path

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.app import _NotificationDispatchPreferencesReader
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def _make_reader(
    settings_store: AccountSettingsStore, session_store: MultiAccountSessionStore | None = None
) -> _NotificationDispatchPreferencesReader:
    return _NotificationDispatchPreferencesReader(
        minds_config=None, account_settings_store=settings_store, session_store=session_store
    )


def test_an_entry_with_an_account_reads_that_accounts_master_toggle(tmp_path: Path) -> None:
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")
    settings_store.apply_settings_values("user-off", {"notifications_enabled": False})
    reader = _make_reader(settings_store)

    assert reader("user-off", "").is_enabled is False
    assert reader("user-other", "").is_enabled is True


def test_a_workspace_entry_reads_its_owning_accounts_toggle(tmp_path: Path) -> None:
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")
    settings_store.apply_settings_values("user-owner", {"notifications_enabled": False})
    session_store = make_session_store_for_test(tmp_path / "sessions")
    record_store = session_store.record_store
    assert record_store is not None
    agent_id = str(AgentId.generate())
    record_store.upsert_local_record(
        "user-owner",
        "owner@example.com",
        ReplicaRecord(host_id=str(HostId.generate()), agent_id=agent_id, display_name="ws"),
    )
    reader = _make_reader(settings_store, session_store)

    assert reader("", agent_id).is_enabled is False


def test_a_workspace_no_record_claims_reads_the_device_record(tmp_path: Path) -> None:
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")
    settings_store.apply_settings_values("local", {"notifications_enabled": False})
    session_store = make_session_store_for_test(tmp_path / "sessions")
    reader = _make_reader(settings_store, session_store)

    assert reader("", str(AgentId.generate())).is_enabled is False
