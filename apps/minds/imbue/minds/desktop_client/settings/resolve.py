"""The one resolution function: a declared setting's value for a context (spec section 4.4).

Resolution order, earliest wins: the override the setting declares (the
reading user's, then the workspace's, for the workspace-held kinds; this
install's own per-workspace value for the install-held kind), then the value
under the setting's scope, then the default (which the carrier model already
applied when the stored value was absent). Every account-keyed reader in the
desktop goes through the typed functions here, so where the carriers are
loaded from can move (as phase 3A moved the account side onto the synced
settings replicas) without touching any reader.

The workspace-held override maps are still empty: the one such override in
effect (the per-workspace update schedule) is armed into the workspace and
consulted there, and keybindings arrive with phase 3C. The install-held map
is live: ``storage.py`` fills it from the workspace's install record
(``specs/workspace-microphone-permission/spec.md``, section 5.3).
"""

from collections.abc import Mapping
from typing import Final
from typing import assert_never

from pydantic import Field
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.settings.schema import ALLOW_AGENT_COMMAND_EXECUTION
from imbue.minds.desktop_client.settings.schema import AUTO_UPDATE_PROMPT_DECISION
from imbue.minds.desktop_client.settings.schema import AUTO_UPDATE_WORKSPACES
from imbue.minds.desktop_client.settings.schema import AccountSettings
from imbue.minds.desktop_client.settings.schema import CLIENT_PERMISSIONS
from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import ClientPermissionSource
from imbue.minds.desktop_client.settings.schema import IMBUE_DEFAULT_CLIENT_PERMISSIONS
from imbue.minds.desktop_client.settings.schema import InstallSettings
from imbue.minds.desktop_client.settings.schema import KEYBINDINGS
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
    workspace's; ``install_workspace_override_by_key`` holds the values this
    install keeps for the workspace, which only the install-held kind reads. A
    read outside any workspace passes none of them.
    """

    install: InstallSettings = Field(description="The install-keyed record")
    account: AccountSettings = Field(description="The account-keyed record the context's account resolves from")
    workspace_override_by_key: Mapping[str, object] = Field(
        default={}, description="Workspace-held values, by setting key"
    )
    user_override_by_key: Mapping[str, object] = Field(
        default={}, description="The reading user's workspace values, by setting key"
    )
    install_workspace_override_by_key: Mapping[str, object] = Field(
        default={}, description="This install's values for the context's workspace, by setting key"
    )


def resolve_setting(
    declaration: SettingDeclaration,
    context: SettingsResolutionContext,
    # The value under the setting's scope: the carrier field the typed caller
    # read, so the scope read stays a statically-checked attribute access.
    scope_value: object,
) -> object:
    """The declared setting's value for this context, by the spec's order."""
    # The override maps the declaration lets this read consult, nearest first.
    match declaration.override_kind:
        case SettingOverrideKind.NONE:
            override_maps: tuple[Mapping[str, object], ...] = ()
        case SettingOverrideKind.WORKSPACE:
            override_maps = (context.workspace_override_by_key,)
        case SettingOverrideKind.WORKSPACE_PER_USER:
            override_maps = (context.user_override_by_key, context.workspace_override_by_key)
        case SettingOverrideKind.INSTALL_WORKSPACE:
            override_maps = (context.install_workspace_override_by_key,)
        case _:
            assert_never(declaration.override_kind)
    for override_by_key in override_maps:
        if declaration.key in override_by_key:
            return override_by_key[declaration.key]
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


def resolve_allow_agent_command_execution(context: SettingsResolutionContext) -> bool:
    return _require_bool(
        ALLOW_AGENT_COMMAND_EXECUTION.key,
        resolve_setting(ALLOW_AGENT_COMMAND_EXECUTION, context, context.install.allow_agent_command_execution),
    )


# The keybindings value's shape: app name, then action id, then the assigned chords.
_KEYBINDINGS_SHAPE: Final[TypeAdapter[dict[str, dict[str, tuple[str, ...]]]]] = TypeAdapter(
    dict[str, dict[str, tuple[str, ...]]]
)


def resolve_keybindings(context: SettingsResolutionContext) -> dict[str, dict[str, tuple[str, ...]]]:
    """The context's keybinding assignments, by app name then action id (only ones differing from defaults)."""
    value = resolve_setting(KEYBINDINGS, context, context.account.keybindings)
    try:
        return _KEYBINDINGS_SHAPE.validate_python(value)
    except ValidationError as e:
        raise SettingValueError(f"The {KEYBINDINGS.key} setting resolved to {value!r}, not chord assignments") from e


def resolve_sign_in_browser(context: SettingsResolutionContext) -> str | None:
    value = resolve_setting(SIGN_IN_BROWSER, context, context.install.sign_in_browser)
    if value is None or isinstance(value, str):
        return value
    raise SettingValueError(f"The {SIGN_IN_BROWSER.key} setting resolved to {value!r}, not a browser id")


class ResolvedClientPermission(FrozenModel):
    """One client permission's decision in effect, and the level of the order that decided it."""

    decision: ClientPermissionDecision = Field(description="The decision in effect for the workspace")
    source: ClientPermissionSource = Field(description="Which level of the resolution order answered")


_CLIENT_PERMISSIONS_SHAPE: Final[TypeAdapter[dict[str, ClientPermissionDecision]]] = TypeAdapter(
    dict[str, ClientPermissionDecision]
)


def _require_client_permissions(value: object) -> dict[str, ClientPermissionDecision]:
    try:
        return _CLIENT_PERMISSIONS_SHAPE.validate_python(value)
    except ValidationError as e:
        raise SettingValueError(
            f"The {CLIENT_PERMISSIONS.key} setting resolved to {value!r}, not decisions by permission name"
        ) from e


def resolve_client_permission(
    name: ClientPermissionName, context: SettingsResolutionContext
) -> ResolvedClientPermission:
    """The decision in effect for one client permission in the context's workspace (spec section 5.3).

    Earliest wins: this install's value for the workspace, when its record
    holds an entry for the name; then the install's value, which in phase 1 is
    the Imbue default by construction; then the Imbue default itself, for a
    name the install record lacks. The setting's value is a mapping, so the
    order runs per name rather than per mapping: a workspace record that names
    other permissions still defers on this one.
    """
    held_for_workspace = context.install_workspace_override_by_key.get(CLIENT_PERMISSIONS.key)
    if held_for_workspace is not None:
        workspace_decision = _require_client_permissions(held_for_workspace).get(name.value)
        if workspace_decision is not None:
            return ResolvedClientPermission(decision=workspace_decision, source=ClientPermissionSource.WORKSPACE)
    install_decision = _require_client_permissions(context.install.client_permissions).get(name.value)
    return ResolvedClientPermission(
        decision=install_decision if install_decision is not None else IMBUE_DEFAULT_CLIENT_PERMISSIONS[name],
        source=ClientPermissionSource.DEFAULT,
    )
