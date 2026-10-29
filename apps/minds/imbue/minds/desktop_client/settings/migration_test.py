from pathlib import Path
from uuid import uuid4

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.settings.migration import migrate_account_keyed_settings


def _write_premigration_config(config_dir: Path) -> MindsConfig:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        "notifications_enabled = false\n"
        'notification_style = "cards"\n'
        "[updates]\n"
        "window_start_hour = 23\n"
        "window_end_hour = 3\n"
        "auto_update_workspaces = true\n"
    )
    return MindsConfig(data_dir=config_dir)


def test_migration_seeds_the_device_and_every_account_then_deletes_the_moved_keys(tmp_path: Path) -> None:
    user_id = uuid4().hex
    minds_config = _write_premigration_config(tmp_path / "config")
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")

    is_migrated = migrate_account_keyed_settings(minds_config, settings_store, [user_id])

    assert is_migrated is True
    for seeded_id in ("local", user_id):
        seeded = settings_store.read_settings(seeded_id)
        assert (seeded.update_window_start_hour, seeded.update_window_end_hour) == (23, 3)
        assert seeded.auto_update_workspaces is True
        assert seeded.notifications_enabled is False
    # The seeds are clean: the first pull decides whether the server's document wins.
    account_record = settings_store.get_record(user_id, "default")
    assert account_record is not None
    assert (account_record.is_dirty, account_record.revision) == (False, 0)
    raw = (tmp_path / "config" / "config.toml").read_text()
    assert "notifications_enabled" not in raw
    assert "window_start_hour" not in raw
    # The install-keyed style stays where it was.
    assert "notification_style" in raw


def test_migration_is_a_no_op_once_the_keys_are_gone(tmp_path: Path) -> None:
    user_id = uuid4().hex
    minds_config = _write_premigration_config(tmp_path / "config")
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")
    migrate_account_keyed_settings(minds_config, settings_store, [user_id])
    settings_store.apply_settings_values(user_id, {"auto_update_workspaces": False})

    assert migrate_account_keyed_settings(minds_config, settings_store, [user_id]) is False

    # The second run changed nothing: the post-migration edit stands.
    assert settings_store.read_settings(user_id).auto_update_workspaces is False


def test_migration_never_overwrites_an_account_record_that_already_exists(tmp_path: Path) -> None:
    user_id = uuid4().hex
    minds_config = _write_premigration_config(tmp_path / "config")
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")
    settings_store.apply_settings_values(user_id, {"auto_update_workspaces": False})

    migrate_account_keyed_settings(minds_config, settings_store, [user_id])

    assert settings_store.read_settings(user_id).auto_update_workspaces is False


def test_migration_waits_when_the_signed_in_accounts_could_not_be_listed(tmp_path: Path) -> None:
    """A failed account listing (None) must not delete the moved keys: the accounts' seeds would be lost for good."""
    user_id = uuid4().hex
    minds_config = _write_premigration_config(tmp_path / "config")
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")

    assert migrate_account_keyed_settings(minds_config, settings_store, None) is False

    assert settings_store.get_record("local", "default") is None
    assert "notifications_enabled" in (tmp_path / "config" / "config.toml").read_text()
    # The next start, with a readable listing, still migrates everything.
    assert migrate_account_keyed_settings(minds_config, settings_store, [user_id]) is True
    assert settings_store.read_settings(user_id).notifications_enabled is False


def test_migration_does_nothing_on_a_fresh_install(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    settings_store = AccountSettingsStore(data_dir=tmp_path / "config")

    assert migrate_account_keyed_settings(minds_config, settings_store, []) is False

    assert settings_store.get_record("local", "default") is None
