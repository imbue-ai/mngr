from pathlib import Path

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.storage import load_settings_context


def test_the_context_carries_the_stored_values_of_the_named_account(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path)
    settings_store = AccountSettingsStore(data_dir=tmp_path)
    settings_store.apply_settings_values(
        "user-1",
        {
            "update_window_start_hour": 22,
            "update_window_end_hour": 6,
            "auto_update_workspaces": True,
            "auto_update_prompt_decision": "declined",
            "notifications_enabled": False,
        },
    )
    minds_config.set_report_unexpected_errors(False)
    minds_config.set_sign_in_browser_id("/Applications/Firefox.app")

    context = load_settings_context(minds_config, settings_store, "user-1")

    assert context.account == AccountSettings(
        update_window_start_hour=22,
        update_window_end_hour=6,
        auto_update_workspaces=True,
        auto_update_prompt_decision=AutoUpdatePromptDecision.DECLINED,
        notifications_enabled=False,
    )
    assert context.install == InstallSettings(
        sign_in_browser="/Applications/Firefox.app",
        report_unexpected_errors=False,
        notification_style=NotificationStyle.BOTH,
    )
    assert context.workspace_override_by_key == {}
    assert context.user_override_by_key == {}


def test_each_account_resolves_from_its_own_record(tmp_path: Path) -> None:
    settings_store = AccountSettingsStore(data_dir=tmp_path)
    settings_store.apply_settings_values("user-1", {"auto_update_workspaces": True})

    for_owner = load_settings_context(None, settings_store, "user-1")
    for_other = load_settings_context(None, settings_store, "user-2")

    assert for_owner.account.auto_update_workspaces is True
    assert for_other.account.auto_update_workspaces is False


def test_without_stores_the_context_is_the_declared_defaults() -> None:
    context = load_settings_context(None, None, "local")

    assert context.account == AccountSettings()
    assert context.install == InstallSettings()


def test_a_fresh_install_reads_as_the_declared_defaults(tmp_path: Path) -> None:
    """The stores' fallbacks and the carriers' declared defaults must agree,
    or a storage move would silently change answers."""
    context = load_settings_context(MindsConfig(data_dir=tmp_path), AccountSettingsStore(data_dir=tmp_path), "local")

    assert context.account == AccountSettings()
    assert context.install == InstallSettings()
