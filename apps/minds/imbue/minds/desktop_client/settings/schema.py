"""The settings model's one registry: every declared setting, its scope, and its override rule.

Phases 2 and 3A of ``specs/user-profile-state/spec.md``: every setting declares
exactly one scope (install-keyed or account-keyed) and one override kind, and
the two carrier models here are the shape the values travel in -- loaded by
``storage.py`` from ``config.toml`` (install-keyed) and from the per-account
replica of the synced ``settings`` account document, whose metadata keys these
models fix (account-keyed). Settings with bespoke semantics beyond plain
resolution (the consent flags, the per-provider regions, the Electron files)
are deliberately not declared: they stay on their own accessors.

Install-keyed settings have no workspace-held override, so reading them
straight from storage already is their resolution; account-keyed readers, and
the one install-keyed setting this install holds per workspace (the client
permissions, ``specs/workspace-microphone-permission/spec.md``), go through
``resolve.py``, which is what let phase 3A move their storage without touching
behavior.
"""

from collections.abc import Mapping
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.minds_config import AutoUpdatePromptDecision
from imbue.minds.desktop_client.minds_config import DEFAULT_NOTIFICATION_STYLE
from imbue.minds.desktop_client.minds_config import DEFAULT_UPDATE_WINDOW
from imbue.minds.desktop_client.minds_config import NotificationStyle

# The newest settings-record shape this build understands. A record carrying a
# higher format was written by a newer client: it is still resolved from, but
# never written (spec section 4.3).
SUPPORTED_SETTINGS_FORMAT: Final[int] = 1


class SettingScope(UpperCaseStrEnum):
    """Where a setting's one value lives: per install, or per account (``local`` included)."""

    INSTALL = auto()
    ACCOUNT = auto()


class SettingOverrideKind(UpperCaseStrEnum):
    """Who may hold a value per workspace: nobody, the workspace itself (for everyone or per user), or this install."""

    NONE = auto()
    WORKSPACE = auto()
    WORKSPACE_PER_USER = auto()
    # This install holds one value per workspace; nothing syncs, nothing enters the workspace.
    INSTALL_WORKSPACE = auto()


class SettingDeclaration(FrozenModel):
    """One setting's place in the model: its key names the carrier field holding its value and default."""

    key: str = Field(description="The setting's name: a carrier model field, and the future document key")
    scope: SettingScope = Field(description="Which carrier holds the value")
    override_kind: SettingOverrideKind = Field(description="Whether a workspace may override it")


class ClientPermissionName(LowerCaseStrEnum):
    """A capability of this computer a workspace's pages may use, named as its W3C Permissions Policy feature.

    The lowercase values are the wire strings the client-permission routes serve
    and accept verbatim, the keys of the per-workspace records, and the name a
    page passes to ``document.featurePolicy.allowsFeature``.
    """

    MICROPHONE = auto()


class ClientPermissionDecision(LowerCaseStrEnum):
    """What this install decided about one client permission for one workspace.

    The lowercase values are the wire strings the client-permission routes serve
    and accept verbatim (and what the per-workspace records store).
    """

    GRANTED = auto()
    DENIED = auto()
    ASK = auto()


class ClientPermissionSource(LowerCaseStrEnum):
    """Which level of the resolution order decided a client permission (spec section 5.3)."""

    WORKSPACE = auto()
    DEFAULT = auto()


# The decision Studio ships for each declared permission, where nothing else
# decides it: every resolution ends here (spec D4).
IMBUE_DEFAULT_CLIENT_PERMISSIONS: Final[Mapping[ClientPermissionName, ClientPermissionDecision]] = {
    ClientPermissionName.MICROPHONE: ClientPermissionDecision.ASK,
}
# The same table keyed the way the carriers hold it: by the name string, so a
# name a newer build declared round-trips through an older one unread.
_IMBUE_DEFAULT_CLIENT_PERMISSIONS_BY_NAME: Final[Mapping[str, ClientPermissionDecision]] = {
    name.value: decision for name, decision in IMBUE_DEFAULT_CLIENT_PERMISSIONS.items()
}
_CLIENT_PERMISSION_DECISION_VALUES: Final[frozenset[str]] = frozenset(
    decision.value for decision in ClientPermissionDecision
)


def _is_client_permission_decision(value: object) -> bool:
    return isinstance(value, str) and value in _CLIENT_PERMISSION_DECISION_VALUES


def _without_unparseable_decisions(raw: object) -> object:
    """Drop the entries whose decision this build cannot parse, one warning each; the rest validate as usual."""
    if not isinstance(raw, Mapping):
        return raw
    kept: dict[object, object] = {}
    for name, decision in raw.items():
        if _is_client_permission_decision(decision):
            kept[name] = decision
        else:
            logger.warning(
                "Dropping the client permission entry {!r}: {!r} is not a decision this build knows", name, decision
            )
    return kept


class InstallSettings(FrozenModel):
    """The install-keyed settings record: values that describe this device or this install.

    Unknown keys are kept (``extra="allow"``) so a record a newer client wrote
    round-trips without losing what this build does not know.
    """

    model_config = ConfigDict(frozen=True, extra="allow", arbitrary_types_allowed=False)

    settings_format: int = Field(default=SUPPORTED_SETTINGS_FORMAT, description="The record's shape version")
    sign_in_browser: str | None = Field(
        default=None, description="The browser provider sign-ins open in; None for the default browser"
    )
    report_unexpected_errors: bool = Field(
        default=True, description="Whether unexpected errors are reported automatically"
    )
    notification_style: NotificationStyle = Field(
        default=DEFAULT_NOTIFICATION_STYLE, description="Delivery style for feed-backed notifications"
    )
    allow_agent_command_execution: bool = Field(
        default=False,
        description=(
            "Whether an approved workspace's agents may run commands on this device as the user; "
            "install-keyed on purpose, so enabling it on one device never enables it on another"
        ),
    )
    client_permissions: Mapping[str, ClientPermissionDecision] = Field(
        default=_IMBUE_DEFAULT_CLIENT_PERMISSIONS_BY_NAME,
        description=(
            "The decision in effect for each client permission (by name) where a workspace holds none of its own; "
            "nothing writes it yet, so it is the Imbue default table (spec section 5.2)"
        ),
    )


class AccountSettings(FrozenModel):
    """The account-keyed settings record: the person's preferences, one record per account id.

    Each account's record is synced as the ``settings`` account document, whose
    metadata keys are exactly these field names. Unknown keys are kept
    (``extra="allow"``) so a newer client's record round-trips.
    """

    model_config = ConfigDict(frozen=True, extra="allow", arbitrary_types_allowed=False)

    settings_format: int = Field(default=SUPPORTED_SETTINGS_FORMAT, description="The record's shape version")
    update_window_start_hour: int = Field(
        default=DEFAULT_UPDATE_WINDOW[0], ge=0, le=23, description="Local hour scheduled updates may start"
    )
    update_window_end_hour: int = Field(
        default=DEFAULT_UPDATE_WINDOW[1], ge=0, le=23, description="Local hour scheduled updates stop"
    )
    auto_update_workspaces: bool = Field(
        default=False, description="Whether Imbue Studio arms every behind workspace's update itself"
    )
    auto_update_prompt_decision: AutoUpdatePromptDecision | None = Field(
        default=None, description="How the one-time auto-update offer was answered; None while it never was"
    )
    notifications_enabled: bool = Field(
        default=True, description="Master notifications toggle (gates every OS nudge the app sends)"
    )
    keybindings: dict[str, dict[str, tuple[str, ...]]] = Field(
        default_factory=dict,
        description=(
            "Keyboard shortcut assignments for the workspace's built-in apps, by app name then action id; "
            "only assignments that differ from the app's defaults are stored (spec section 10.1)"
        ),
    )


class WorkspaceInstallSettings(FrozenModel):
    """Values this install holds for one workspace: this computer's decisions about that workspace.

    One record per workspace, held by the install and never synced
    (``specs/workspace-microphone-permission/spec.md``, D1). Unknown keys are
    kept (``extra="allow"``), and so is an entry naming a permission this build
    does not declare: both round-trip a newer build's record. Only an entry
    whose decision cannot be parsed is dropped, with a warning, so one bad value
    does not cost the workspace its other answers.
    """

    model_config = ConfigDict(frozen=True, extra="allow", arbitrary_types_allowed=False)

    settings_format: int = Field(default=SUPPORTED_SETTINGS_FORMAT, description="The record's shape version")
    client_permissions: Mapping[str, ClientPermissionDecision] = Field(
        default_factory=dict,
        description="This workspace's own decisions, by permission name; an absent name defers to the next level",
    )

    @field_validator("client_permissions", mode="before")
    @classmethod
    def _drop_unparseable_decisions(cls, raw: object) -> object:
        return _without_unparseable_decisions(raw)


# The update window hours are the one declared override in effect: the
# per-workspace schedule under data/.state/update-apply/ wins inside that
# workspace (spec S10). It is armed into the workspace and consulted there,
# so the desktop's resolution context carries no live override source yet.
UPDATE_WINDOW_START_HOUR: Final[SettingDeclaration] = SettingDeclaration(
    key="update_window_start_hour", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.WORKSPACE
)
UPDATE_WINDOW_END_HOUR: Final[SettingDeclaration] = SettingDeclaration(
    key="update_window_end_hour", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.WORKSPACE
)
AUTO_UPDATE_WORKSPACES: Final[SettingDeclaration] = SettingDeclaration(
    key="auto_update_workspaces", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.NONE
)
AUTO_UPDATE_PROMPT_DECISION: Final[SettingDeclaration] = SettingDeclaration(
    key="auto_update_prompt_decision", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.NONE
)
NOTIFICATIONS_ENABLED: Final[SettingDeclaration] = SettingDeclaration(
    key="notifications_enabled", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.NONE
)
# The one per-user override in the model (spec S10): a workspace may hold a
# keybinding per user, edited from inside the workspace, which wins there for
# that user only. The override files live in the workspace and are consulted by
# the template's workspace_profile library, not by the desktop's own reads.
KEYBINDINGS: Final[SettingDeclaration] = SettingDeclaration(
    key="keybindings", scope=SettingScope.ACCOUNT, override_kind=SettingOverrideKind.WORKSPACE_PER_USER
)
SIGN_IN_BROWSER: Final[SettingDeclaration] = SettingDeclaration(
    key="sign_in_browser", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
REPORT_UNEXPECTED_ERRORS: Final[SettingDeclaration] = SettingDeclaration(
    key="report_unexpected_errors", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
NOTIFICATION_STYLE: Final[SettingDeclaration] = SettingDeclaration(
    key="notification_style", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
ALLOW_AGENT_COMMAND_EXECUTION: Final[SettingDeclaration] = SettingDeclaration(
    key="allow_agent_command_execution", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
# This computer's decisions about a workspace live in its
# WorkspaceInstallSettings record and win over the install's value. The
# override is held by the install, never by the workspace, so nothing inside a
# workspace depends on the device (spec D5).
CLIENT_PERMISSIONS: Final[SettingDeclaration] = SettingDeclaration(
    key="client_permissions", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.INSTALL_WORKSPACE
)

SETTING_DECLARATIONS: Final[tuple[SettingDeclaration, ...]] = (
    UPDATE_WINDOW_START_HOUR,
    UPDATE_WINDOW_END_HOUR,
    AUTO_UPDATE_WORKSPACES,
    AUTO_UPDATE_PROMPT_DECISION,
    NOTIFICATIONS_ENABLED,
    KEYBINDINGS,
    SIGN_IN_BROWSER,
    REPORT_UNEXPECTED_ERRORS,
    NOTIFICATION_STYLE,
    ALLOW_AGENT_COMMAND_EXECUTION,
    CLIENT_PERMISSIONS,
)


def is_settings_format_supported(settings_format: int) -> bool:
    """Whether this build may write a record of this shape; a too-new record stays read-only."""
    return settings_format <= SUPPORTED_SETTINGS_FORMAT
