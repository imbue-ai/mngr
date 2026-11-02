import pytest
from pydantic import ValidationError

from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.schema import SETTING_DECLARATIONS
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.settings.schema import SettingOverrideKind
from imbue.minds.desktop_client.settings.schema import SettingScope
from imbue.minds.desktop_client.settings.schema import is_settings_format_supported


def test_every_declared_setting_is_a_field_of_its_scopes_carrier() -> None:
    """The declaration's key is how resolution reads the carrier, so the two may never drift."""
    account_fields = set(AccountSettings.model_fields) - {"settings_format"}
    install_fields = set(InstallSettings.model_fields) - {"settings_format"}
    declared_account = {d.key for d in SETTING_DECLARATIONS if d.scope is SettingScope.ACCOUNT}
    declared_install = {d.key for d in SETTING_DECLARATIONS if d.scope is SettingScope.INSTALL}
    assert declared_account == account_fields
    assert declared_install == install_fields


def test_no_install_keyed_setting_declares_an_override() -> None:
    """Spec section 4.2: nothing inside a workspace depends on the device."""
    for declaration in SETTING_DECLARATIONS:
        if declaration.scope is SettingScope.INSTALL:
            assert declaration.override_kind is SettingOverrideKind.NONE, declaration.key


def test_the_account_document_key_names_are_pinned() -> None:
    """These field names become the synced settings document's metadata keys in
    phase 3 (and the template mirror's), so a rename must be a deliberate,
    migrated change, never an incidental one."""
    assert set(AccountSettings.model_fields) == {
        "settings_format",
        "update_window_start_hour",
        "update_window_end_hour",
        "auto_update_workspaces",
        "auto_update_prompt_decision",
        "notifications_enabled",
        "keybindings",
    }
    assert set(InstallSettings.model_fields) == {
        "settings_format",
        "sign_in_browser",
        "report_unexpected_errors",
        "notification_style",
    }


def test_the_carriers_round_trip_unknown_keys() -> None:
    """A record a newer client wrote keeps the keys this build does not know."""
    record = AccountSettings.model_validate({"notifications_enabled": False, "future_key": "kept"})
    dumped = record.model_dump()
    assert dumped["future_key"] == "kept"
    assert dumped["notifications_enabled"] is False


def test_a_too_new_settings_format_reads_as_not_writable() -> None:
    assert is_settings_format_supported(SUPPORTED_SETTINGS_FORMAT) is True
    assert is_settings_format_supported(SUPPORTED_SETTINGS_FORMAT + 1) is False


def test_the_carriers_refuse_an_out_of_range_hour() -> None:
    with pytest.raises(ValidationError):
        AccountSettings.model_validate({"update_window_start_hour": 24})


def test_the_keybindings_value_round_trips_app_action_chords() -> None:
    record = AccountSettings.model_validate({"keybindings": {"shell": {"open-launcher": ["ctrl+space"]}}})
    assert record.keybindings == {"shell": {"open-launcher": ("ctrl+space",)}}


def test_the_carriers_refuse_a_keybindings_value_that_is_not_app_action_chords() -> None:
    with pytest.raises(ValidationError):
        AccountSettings.model_validate({"keybindings": {"shell": ["ctrl+space"]}})
