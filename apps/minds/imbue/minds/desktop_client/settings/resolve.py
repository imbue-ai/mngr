"""The one resolution function: a declared setting's value for a context (spec section 4.4).

Resolution order, earliest wins: the workspace's override (per user when the
setting declares it), then the value under the setting's scope, then the
default (which the carrier model already applied when the stored value was
absent). Every account-keyed reader in the desktop goes through the typed
functions here, so where the carriers are loaded from can move (as phase 3A
moved the account side onto the synced settings replicas) without touching
any reader.

The override maps are still empty: the one override in effect (the
per-workspace update schedule) is armed into the workspace and consulted
there, and keybindings arrive with phase 3C. The arms exist now so the order
is tested and later phases only have to supply the maps.
"""

from collections.abc import Mapping

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.settings.schema import AUTO_UPDATE_PROMPT_DECISION
from imbue.minds.desktop_client.settings.schema import AUTO_UPDATE_WORKSPACES
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.schema import NOTIFICATIONS_ENABLED
from imbue.minds.desktop_client.settings.schema import NOTIFICATION_STYLE
from imbue.minds.desktop_client.settings.schema import REPORT_UNEXPECTED_ERRORS
from imbue.minds.desktop_client.settings.schema import SIGN_IN_BROWSER
from imbue.minds.desktop_client.settings.schema import SettingDeclaration
from imbue.minds.desktop_client.settings.schema import SettingOverrideKind
from imbue.minds.desktop_client.settings.schema import UPDATE_WINDOW_END_HOUR
from imbue.minds.desktop_client.settings.schema import UPDATE_WINDOW_START_HOUR
from imbue.minds.errors import SettingValueError


class SettingsResolutionContext(FrozenModel):
    """What one resolution sees: the two loaded records, and the overrides that reach this read.

    ``workspace_override_by_key`` holds the workspace's own values for the
    settings whose declaration allows one; ``user_override_by_key`` holds the
    reading user's values for the per-user kind, which wins over the
    workspace's. A read outside any workspace passes neither.
    """

    install: InstallSettings = Field(description="The install-keyed record")
    account: AccountSettings = Field(description="The account-keyed record the context's account resolves from")
    workspace_override_by_key: Mapping[str, object] = Field(
        default={}, description="Workspace-held values, by setting key"
    )
    user_override_by_key: Mapping[str, object] = Field(
        default={}, description="The reading user's workspace values, by setting key"
    )


def resolve_setting(
    declaration: SettingDeclaration,
    context: SettingsResolutionContext,
    # The value under the setting's scope: the carrier field the typed caller
    # read, so the scope read stays a statically-checked attribute access.
    scope_value: object,
) -> object:
    """The declared setting's value for this context, by the spec's order."""
    if declaration.override_kind is SettingOverrideKind.WORKSPACE_PER_USER:
        if declaration.key in context.user_override_by_key:
            return context.user_override_by_key[declaration.key]
    if declaration.override_kind is not SettingOverrideKind.NONE:
        if declaration.key in context.workspace_override_by_key:
            return context.workspace_override_by_key[declaration.key]
    return scope_value


def _require_bool(key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise SettingValueError(f"The {key} setting resolved to {value!r}, not a boolean")
    return value


def _require_hour(key: str, value: object) -> int:
    # bool is an int subclass; a stored ``true`` is not hour one.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
        raise SettingValueError(f"The {key} setting resolved to {value!r}, not an hour")
    return value


def resolve_update_window(context: SettingsResolutionContext) -> tuple[int, int]:
    """The local-clock hours [start, end) scheduled updates may run in."""
    return (
        _require_hour(
            UPDATE_WINDOW_START_HOUR.key,
            resolve_setting(UPDATE_WINDOW_START_HOUR, context, context.account.update_window_start_hour),
        ),
        _require_hour(
            UPDATE_WINDOW_END_HOUR.key,
            resolve_setting(UPDATE_WINDOW_END_HOUR, context, context.account.update_window_end_hour),
        ),
    )


def resolve_auto_update_workspaces(context: SettingsResolutionContext) -> bool:
    return _require_bool(
        AUTO_UPDATE_WORKSPACES.key,
        resolve_setting(AUTO_UPDATE_WORKSPACES, context, context.account.auto_update_workspaces),
    )


def resolve_auto_update_prompt_decision(context: SettingsResolutionContext) -> AutoUpdatePromptDecision | None:
    value = resolve_setting(AUTO_UPDATE_PROMPT_DECISION, context, context.account.auto_update_prompt_decision)
    if value is None or isinstance(value, AutoUpdatePromptDecision):
        return value
    raise SettingValueError(f"The {AUTO_UPDATE_PROMPT_DECISION.key} setting resolved to {value!r}, not a decision")


def resolve_notifications_enabled(context: SettingsResolutionContext) -> bool:
    return _require_bool(
        NOTIFICATIONS_ENABLED.key,
        resolve_setting(NOTIFICATIONS_ENABLED, context, context.account.notifications_enabled),
    )


def resolve_notification_style(context: SettingsResolutionContext) -> NotificationStyle:
    value = resolve_setting(NOTIFICATION_STYLE, context, context.install.notification_style)
    if not isinstance(value, NotificationStyle):
        raise SettingValueError(f"The {NOTIFICATION_STYLE.key} setting resolved to {value!r}, not a style")
    return value


def resolve_report_unexpected_errors(context: SettingsResolutionContext) -> bool:
    return _require_bool(
        REPORT_UNEXPECTED_ERRORS.key,
        resolve_setting(REPORT_UNEXPECTED_ERRORS, context, context.install.report_unexpected_errors),
    )


def resolve_sign_in_browser(context: SettingsResolutionContext) -> str | None:
    value = resolve_setting(SIGN_IN_BROWSER, context, context.install.sign_in_browser)
    if value is None or isinstance(value, str):
        return value
    raise SettingValueError(f"The {SIGN_IN_BROWSER.key} setting resolved to {value!r}, not a browser id")
