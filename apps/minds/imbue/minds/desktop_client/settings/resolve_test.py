import pytest

from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.resolve import resolve_auto_update_prompt_decision
from imbue.minds.desktop_client.settings.resolve import resolve_auto_update_workspaces
from imbue.minds.desktop_client.settings.resolve import resolve_notification_style
from imbue.minds.desktop_client.settings.resolve import resolve_notifications_enabled
from imbue.minds.desktop_client.settings.resolve import resolve_report_unexpected_errors
from imbue.minds.desktop_client.settings.resolve import resolve_setting
from imbue.minds.desktop_client.settings.resolve import resolve_sign_in_browser
from imbue.minds.desktop_client.settings.resolve import resolve_update_window
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.schema import SettingDeclaration
from imbue.minds.desktop_client.settings.schema import SettingOverrideKind
from imbue.minds.desktop_client.settings.schema import SettingScope
from imbue.minds.errors import SettingValueError


def _context(
    install: InstallSettings | None = None,
    account: AccountSettings | None = None,
    workspace_override_by_key: dict[str, object] | None = None,
    user_override_by_key: dict[str, object] | None = None,
) -> SettingsResolutionContext:
    return SettingsResolutionContext(
        install=install if install is not None else InstallSettings(),
        account=account if account is not None else AccountSettings(),
        workspace_override_by_key=workspace_override_by_key or {},
        user_override_by_key=user_override_by_key or {},
    )


_WORKSPACE_SETTING = SettingDeclaration(
    key="notifications_enabled", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.WORKSPACE
)
_PER_USER_SETTING = SettingDeclaration(
    key="notifications_enabled", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.WORKSPACE_PER_USER
)
_PLAIN_SETTING = SettingDeclaration(
    key="notifications_enabled", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.NONE
)


def test_the_scope_value_answers_when_nothing_overrides() -> None:
    context = _context(account=AccountSettings(notifications_enabled=False))
    assert resolve_setting(_PLAIN_SETTING, context, context.account.notifications_enabled) is False


def test_the_model_default_answers_when_the_scope_holds_nothing() -> None:
    context = _context()
    assert resolve_setting(_PLAIN_SETTING, context, context.account.notifications_enabled) is True


def test_a_workspace_override_wins_over_the_scope_value_when_declared() -> None:
    context = _context(
        account=AccountSettings(notifications_enabled=True),
        workspace_override_by_key={"notifications_enabled": False},
    )
    assert resolve_setting(_WORKSPACE_SETTING, context, context.account.notifications_enabled) is False


def test_an_undeclared_override_is_ignored() -> None:
    context = _context(workspace_override_by_key={"notifications_enabled": False})
    assert resolve_setting(_PLAIN_SETTING, context, context.account.notifications_enabled) is True


def test_the_users_override_wins_over_the_workspaces_for_the_per_user_kind() -> None:
    context = _context(
        workspace_override_by_key={"notifications_enabled": False},
        user_override_by_key={"notifications_enabled": True},
    )
    assert resolve_setting(_PER_USER_SETTING, context, context.account.notifications_enabled) is True
    # Without the user's own value, the workspace's answers.
    workspace_only = _context(workspace_override_by_key={"notifications_enabled": False})
    assert resolve_setting(_PER_USER_SETTING, workspace_only, workspace_only.account.notifications_enabled) is False


def test_a_user_override_never_answers_the_workspace_wide_kind() -> None:
    context = _context(user_override_by_key={"notifications_enabled": False})
    assert resolve_setting(_WORKSPACE_SETTING, context, context.account.notifications_enabled) is True


def test_an_install_keyed_setting_reads_the_install_carrier() -> None:
    declaration = SettingDeclaration(
        key="report_unexpected_errors", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
    )
    context = _context(install=InstallSettings(report_unexpected_errors=False))
    assert resolve_setting(declaration, context, context.install.report_unexpected_errors) is False


def test_the_typed_resolvers_answer_the_carrier_values() -> None:
    context = _context(
        install=InstallSettings(
            sign_in_browser="/Applications/Firefox.app",
            report_unexpected_errors=False,
            notification_style=NotificationStyle.CARDS,
        ),
        account=AccountSettings(
            update_window_start_hour=22,
            update_window_end_hour=6,
            auto_update_workspaces=True,
            auto_update_prompt_decision=AutoUpdatePromptDecision.ACCEPTED,
            notifications_enabled=False,
        ),
    )
    assert resolve_update_window(context) == (22, 6)
    assert resolve_auto_update_workspaces(context) is True
    assert resolve_auto_update_prompt_decision(context) is AutoUpdatePromptDecision.ACCEPTED
    assert resolve_notifications_enabled(context) is False
    assert resolve_notification_style(context) is NotificationStyle.CARDS
    assert resolve_report_unexpected_errors(context) is False
    assert resolve_sign_in_browser(context) == "/Applications/Firefox.app"


def test_an_overridden_update_window_resolves_from_the_override() -> None:
    context = _context(
        account=AccountSettings(update_window_start_hour=2, update_window_end_hour=5),
        workspace_override_by_key={"update_window_start_hour": 20, "update_window_end_hour": 23},
    )
    assert resolve_update_window(context) == (20, 23)


@pytest.mark.parametrize("bad_hour", [True, "2", 24, -1], ids=["boolean", "string", "too-high", "negative"])
def test_a_malformed_override_value_raises_instead_of_resolving(bad_hour: object) -> None:
    context = _context(workspace_override_by_key={"update_window_start_hour": bad_hour})
    with pytest.raises(SettingValueError):
        resolve_update_window(context)
