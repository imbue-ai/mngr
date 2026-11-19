import pytest
from pydantic import ValidationError

from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import CLIENT_PERMISSIONS
from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import IMBUE_DEFAULT_CLIENT_PERMISSIONS
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.schema import SETTING_DECLARATIONS
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.settings.schema import SettingOverrideKind
from imbue.minds.desktop_client.settings.schema import SettingScope
from imbue.minds.desktop_client.settings.schema import WorkspaceInstallSettings
from imbue.minds.desktop_client.settings.schema import is_settings_format_supported
from imbue.mngr.utils.testing import capture_loguru

# Every override kind a workspace itself may hold a value under (spec section 4.2).
_WORKSPACE_HELD_OVERRIDE_KINDS = frozenset({SettingOverrideKind.WORKSPACE, SettingOverrideKind.WORKSPACE_PER_USER})


def test_every_declared_setting_is_a_field_of_its_scopes_carrier() -> None:
    """The declaration's key is how resolution reads the carrier, so the two may never drift."""
    account_fields = set(AccountSettings.model_fields) - {"settings_format"}
    install_fields = set(InstallSettings.model_fields) - {"settings_format"}
    declared_account = {d.key for d in SETTING_DECLARATIONS if d.scope is SettingScope.ACCOUNT}
    declared_install = {d.key for d in SETTING_DECLARATIONS if d.scope is SettingScope.INSTALL}
    assert declared_account == account_fields
    assert declared_install == install_fields


def test_no_install_keyed_setting_declares_a_workspace_held_override() -> None:
    """Spec section 4.2: nothing inside a workspace depends on the device.

    An install-held per-workspace value is the install's own, so it is the
    one override an install-keyed setting may declare."""
    for declaration in SETTING_DECLARATIONS:
        if declaration.scope is SettingScope.INSTALL:
            assert declaration.override_kind not in _WORKSPACE_HELD_OVERRIDE_KINDS, declaration.key


def test_only_an_install_keyed_setting_may_be_held_per_workspace_by_the_install() -> None:
    """A value this install holds per workspace is this device's, which an account-keyed setting is not."""
    for declaration in SETTING_DECLARATIONS:
        if declaration.override_kind is SettingOverrideKind.INSTALL_WORKSPACE:
            assert declaration.scope is SettingScope.INSTALL, declaration.key
    assert CLIENT_PERMISSIONS.override_kind is SettingOverrideKind.INSTALL_WORKSPACE
    assert CLIENT_PERMISSIONS in SETTING_DECLARATIONS


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
        "allow_agent_command_execution",
        "client_permissions",
    }
    assert set(WorkspaceInstallSettings.model_fields) == {"settings_format", "client_permissions"}


@pytest.mark.witnesses("remote-execution.switch-off-by-default")
def test_agent_command_execution_is_install_keyed_and_off_by_default() -> None:
    """The switch is one device's choice: an account-keyed record would sync it to every device."""
    declaration = next(d for d in SETTING_DECLARATIONS if d.key == "allow_agent_command_execution")
    assert declaration.scope is SettingScope.INSTALL
    assert InstallSettings().allow_agent_command_execution is False


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


def test_every_declared_client_permission_has_an_imbue_default() -> None:
    """Spec D4: resolution ends in an Imbue-set default, so a declared name without a row could never resolve."""
    assert set(IMBUE_DEFAULT_CLIENT_PERMISSIONS) == set(ClientPermissionName)


def test_the_client_permission_wire_values_are_the_lowercase_feature_and_decision_names() -> None:
    """Spec D2 and D3: the stored and served strings are the Permissions Policy feature name and the three decisions."""
    assert [name.value for name in ClientPermissionName] == ["microphone"]
    assert [decision.value for decision in ClientPermissionDecision] == ["granted", "denied", "ask"]


def test_the_install_record_defaults_its_client_permissions_to_the_imbue_table() -> None:
    """Nothing writes the install-level value in phase 1, so it is the Imbue default by construction."""
    assert InstallSettings().client_permissions == {"microphone": "ask"}
    assert InstallSettings().client_permissions == {"microphone": ClientPermissionDecision.ASK}


def test_the_workspace_record_parses_the_decision_strings_into_decisions() -> None:
    record = WorkspaceInstallSettings.model_validate({"client_permissions": {"microphone": "granted"}})
    assert record.client_permissions == {"microphone": ClientPermissionDecision.GRANTED}
    assert record.settings_format == SUPPORTED_SETTINGS_FORMAT


def test_the_workspace_record_drops_an_unparseable_decision_and_keeps_the_rest() -> None:
    """One bad value must not cost the workspace its other answers, nor fail the whole record."""
    with capture_loguru(level="WARNING") as log_output:
        record = WorkspaceInstallSettings.model_validate(
            {"client_permissions": {"microphone": "maybe", "camera": "denied"}, "settings_format": 1}
        )
    assert record.client_permissions == {"camera": ClientPermissionDecision.DENIED}
    assert "maybe" in log_output.getvalue()


def test_the_workspace_record_keeps_a_name_this_build_does_not_declare() -> None:
    """A name a newer build declared round-trips through this one unread (spec section 5.1)."""
    record = WorkspaceInstallSettings.model_validate({"client_permissions": {"geolocation": "granted"}})
    assert record.client_permissions == {"geolocation": ClientPermissionDecision.GRANTED}
    assert record.model_dump(mode="json") == {
        "settings_format": SUPPORTED_SETTINGS_FORMAT,
        "client_permissions": {"geolocation": "granted"},
    }


def test_the_workspace_record_round_trips_unknown_keys() -> None:
    record = WorkspaceInstallSettings.model_validate({"future_key": "kept"})
    assert record.model_dump()["future_key"] == "kept"
    assert record.client_permissions == {}


def test_the_workspace_record_refuses_client_permissions_that_are_not_a_mapping() -> None:
    with pytest.raises(ValidationError):
        WorkspaceInstallSettings.model_validate({"client_permissions": ["microphone"]})
