"""Unit tests for the settings-document store, and (through it) the generic account-document engine.

The push/pull/rebase/tombstone/too-new paths exercised here run in the shared
``AccountDocumentStore`` base, against the fake connector's in-memory
documents semantics -- the same ones the connection store's own tests drive.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Callable
from uuid import uuid4

import pytest

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.account_settings_store import SETTINGS_DOCUMENT_KIND
from imbue.minds.desktop_client.conftest import FAKE_CONNECTOR_URL
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.errors import SettingsRecordTooNewError


class _WriteDuringPushCli(FakeImbueCloudCli):
    """A connector whose push answers only after one more local write has landed (once)."""

    on_document_pushed: Callable[[], None] | None = None

    def sync_document_push(self, account: str, document: Mapping[str, Any]) -> dict[str, Any]:
        stored = super().sync_document_push(account, document)
        if self.on_document_pushed is not None:
            callback, self.on_document_pushed = self.on_document_pushed, None
            callback()
        return stored


def _make_store(tmp_path: Path, cli: FakeImbueCloudCli | None = None) -> AccountSettingsStore:
    return AccountSettingsStore(data_dir=tmp_path / "data", cli=cli)


def _seed_server_settings(cli: FakeImbueCloudCli, email: str, metadata: dict[str, object], revision: int = 1) -> None:
    """Place a settings document on the fake server, as another device would have pushed it."""
    by_doc_id = cli.sync_documents_by_email.setdefault(email, {}).setdefault(SETTINGS_DOCUMENT_KIND, {})
    by_doc_id["default"] = {
        "kind": SETTINGS_DOCUMENT_KIND,
        "doc_id": "default",
        "metadata": dict(metadata),
        "encrypted_payload": None,
        "state": "active",
        "revision": revision,
        "document_format": 1,
    }


def _server_settings_metadata(cli: FakeImbueCloudCli, email: str) -> dict[str, object]:
    document = cli.sync_documents_by_email[email][SETTINGS_DOCUMENT_KIND]["default"]
    metadata = document["metadata"]
    assert isinstance(metadata, dict)
    return metadata


def test_an_account_with_no_record_reads_as_the_declared_defaults(tmp_path: Path) -> None:
    assert _make_store(tmp_path).read_settings(uuid4().hex) == AccountSettings()


def test_a_write_round_trips_and_persists_across_store_instances(tmp_path: Path) -> None:
    user_id = uuid4().hex
    store = _make_store(tmp_path)

    store.apply_settings_values(user_id, {"update_window_start_hour": 22, "update_window_end_hour": 6})

    relaunched = _make_store(tmp_path).read_settings(user_id)
    assert (relaunched.update_window_start_hour, relaunched.update_window_end_hour) == (22, 6)


def test_a_partial_write_changes_only_its_keys_and_keeps_unknown_ones(tmp_path: Path) -> None:
    """A newer client's keys and the untouched fields survive every partial write (the round-trip rule)."""
    user_id = uuid4().hex
    store = _make_store(tmp_path)
    store.seed_if_absent(user_id, {"notifications_enabled": False, "future_key": "kept"})

    store.apply_settings_values(user_id, {"auto_update_prompt_decision": "declined"})

    record = store.get_record(user_id, "default")
    assert record is not None
    assert record.metadata["future_key"] == "kept"
    assert record.metadata["notifications_enabled"] is False
    settings = store.read_settings(user_id)
    assert settings.auto_update_prompt_decision is AutoUpdatePromptDecision.DECLINED
    assert settings.notifications_enabled is False


def test_a_key_never_chosen_stays_absent_from_the_record(tmp_path: Path) -> None:
    """Absence means "never chosen", exactly as it did in config.toml; the carrier's default answers reads."""
    user_id = uuid4().hex
    store = _make_store(tmp_path)

    store.apply_settings_values(user_id, {"auto_update_workspaces": True})

    record = store.get_record(user_id, "default")
    assert record is not None
    assert "notifications_enabled" not in record.metadata
    assert store.read_settings(user_id).notifications_enabled is True


def test_a_record_written_by_a_newer_app_refuses_writes_but_still_resolves(tmp_path: Path) -> None:
    user_id = uuid4().hex
    store = _make_store(tmp_path)
    store.seed_if_absent(user_id, {"settings_format": 2, "auto_update_workspaces": True})

    assert store.read_settings(user_id).auto_update_workspaces is True
    with pytest.raises(SettingsRecordTooNewError):
        store.apply_settings_values(user_id, {"notifications_enabled": False})


def test_a_malformed_record_resolves_from_the_defaults(tmp_path: Path) -> None:
    user_id = uuid4().hex
    store = _make_store(tmp_path)
    store.seed_if_absent(user_id, {"update_window_start_hour": "late"})

    assert store.read_settings(user_id) == AccountSettings()


def test_is_notifications_enabled_chosen_anywhere_sees_every_accounts_record(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.is_notifications_enabled_chosen_anywhere() is False
    store.apply_settings_values(uuid4().hex, {"auto_update_workspaces": True})
    assert store.is_notifications_enabled_chosen_anywhere() is False

    store.apply_settings_values(uuid4().hex, {"notifications_enabled": True})

    assert store.is_notifications_enabled_chosen_anywhere() is True


def test_seed_if_absent_never_overwrites_an_existing_record(tmp_path: Path) -> None:
    user_id = uuid4().hex
    store = _make_store(tmp_path)
    store.apply_settings_values(user_id, {"auto_update_workspaces": True})

    assert store.seed_if_absent(user_id, {"auto_update_workspaces": False}) is False

    assert store.read_settings(user_id).auto_update_workspaces is True


def test_push_dirty_creates_the_document_and_acks_the_record_clean(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(tmp_path, cli)
    store.apply_settings_values(user_id, {"notifications_enabled": False})

    store.push_dirty(user_id, email)

    assert _server_settings_metadata(cli, email)["notifications_enabled"] is False
    record = store.get_record(user_id, "default")
    assert record is not None
    assert record.is_dirty is False
    assert record.revision == 1


def test_a_write_landing_during_a_push_keeps_its_value_and_is_pushed_by_the_next_pass(tmp_path: Path) -> None:
    """The push's CLI round-trip runs outside the lock; a settings PUT landing meanwhile
    must stay dirty with its value rather than be reverted by the push's ack."""
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = _WriteDuringPushCli(connector_url=FAKE_CONNECTOR_URL)
    store = _make_store(tmp_path, cli)
    store.apply_settings_values(user_id, {"auto_update_workspaces": True})
    cli.on_document_pushed = lambda: store.apply_settings_values(user_id, {"notifications_enabled": False})

    store.push_dirty(user_id, email)

    record = store.get_record(user_id, "default")
    assert record is not None
    assert record.is_dirty is True
    assert record.metadata["notifications_enabled"] is False

    # The next pass pushes it, rebasing onto the revision the first push acked.
    store.push_dirty(user_id, email)

    metadata = _server_settings_metadata(cli, email)
    assert metadata["notifications_enabled"] is False
    assert metadata["auto_update_workspaces"] is True
    refreshed = store.get_record(user_id, "default")
    assert refreshed is not None
    assert refreshed.is_dirty is False


def test_a_push_conflict_rebases_once_and_the_local_values_win(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    _seed_server_settings(cli, email, {"settings_format": 1, "auto_update_workspaces": True}, revision=3)
    store = _make_store(tmp_path, cli)
    store.apply_settings_values(user_id, {"auto_update_workspaces": False})

    store.push_dirty(user_id, email)

    assert _server_settings_metadata(cli, email)["auto_update_workspaces"] is False
    record = store.get_record(user_id, "default")
    assert record is not None
    assert (record.is_dirty, record.revision) == (False, 4)


def test_pull_takes_another_devices_document_and_replaces_a_clean_seed(tmp_path: Path) -> None:
    """Spec 7.5: on the first pull, an existing server document wins and the migration seed is dropped."""
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    _seed_server_settings(
        cli, email, {"settings_format": 1, "update_window_start_hour": 1, "update_window_end_hour": 7}
    )
    store = _make_store(tmp_path, cli)
    store.seed_if_absent(user_id, {"update_window_start_hour": 22, "update_window_end_hour": 6})

    outcome = store.pull(user_id, email)

    assert (outcome.is_reached, outcome.is_changed) == (True, True)
    settings = store.read_settings(user_id)
    assert (settings.update_window_start_hour, settings.update_window_end_hour) == (1, 7)


def test_pull_promotes_a_seed_the_server_has_no_document_for(tmp_path: Path) -> None:
    """Spec 7.5: a missing server document is created from the seed on the account's first pull."""
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    store = _make_store(tmp_path, cli)
    store.seed_if_absent(user_id, {"notifications_enabled": False})

    outcome = store.pull(user_id, email)
    store.push_dirty(user_id, email)

    assert outcome.is_reached is True
    assert _server_settings_metadata(cli, email)["notifications_enabled"] is False


def test_pull_keeps_an_unpushed_local_edit_over_the_servers_row(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    _seed_server_settings(cli, email, {"settings_format": 1, "auto_update_workspaces": True})
    store = _make_store(tmp_path, cli)
    store.apply_settings_values(user_id, {"auto_update_workspaces": False})

    store.pull(user_id, email)

    assert store.read_settings(user_id).auto_update_workspaces is False


def test_an_unreachable_connector_leaves_the_replica_as_it_was(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.is_sync_offline = True
    store = _make_store(tmp_path, cli)
    store.seed_if_absent(user_id, {"notifications_enabled": False})

    outcome = store.pull(user_id, email)

    assert (outcome.is_reached, outcome.is_changed) == (False, False)
    assert store.read_settings(user_id).notifications_enabled is False
