"""The settings model's one registry: every declared setting, its scope, and its override rule.

Phases 2 and 3A of ``specs/user-profile-state/spec.md``: every setting declares
exactly one scope (install-keyed or account-keyed) and one override kind, and
the two carrier models here are the shape the values travel in -- loaded by
``storage.py`` from ``config.toml`` (install-keyed) and from the per-account
replica of the synced ``settings`` account document, whose metadata keys these
models fix (account-keyed). Settings with bespoke semantics beyond plain
resolution (the consent flags, the per-provider regions, the Electron files)
are deliberately not declared: they stay on their own accessors.

Install-keyed settings have no override and a single scope, so reading them
straight from storage already is their resolution; only account-keyed readers
must go through ``resolve.py``, which is what let phase 3A move their storage
without touching behavior.
"""

from enum import auto
from typing import Final

from pydantic import ConfigDict
from pydantic import Field

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
    """Whether one workspace may hold its own value, and whether that value is per user."""

    NONE = auto()
    WORKSPACE = auto()
    WORKSPACE_PER_USER = auto()


class SettingDeclaration(FrozenModel):
    """One setting's place in the model: its key names the carrier field holding its value and default."""

    key: str = Field(description="The setting's name: a carrier model field, and the future document key")
    scope: SettingScope = Field(description="Which carrier holds the value")
    override_kind: SettingOverrideKind = Field(description="Whether a workspace may override it")


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
SIGN_IN_BROWSER: Final[SettingDeclaration] = SettingDeclaration(
    key="sign_in_browser", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
REPORT_UNEXPECTED_ERRORS: Final[SettingDeclaration] = SettingDeclaration(
    key="report_unexpected_errors", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)
NOTIFICATION_STYLE: Final[SettingDeclaration] = SettingDeclaration(
    key="notification_style", scope=SettingScope.INSTALL, override_kind=SettingOverrideKind.NONE
)

SETTING_DECLARATIONS: Final[tuple[SettingDeclaration, ...]] = (
    UPDATE_WINDOW_START_HOUR,
    UPDATE_WINDOW_END_HOUR,
    AUTO_UPDATE_WORKSPACES,
    AUTO_UPDATE_PROMPT_DECISION,
    NOTIFICATIONS_ENABLED,
    SIGN_IN_BROWSER,
    REPORT_UNEXPECTED_ERRORS,
    NOTIFICATION_STYLE,
)


def is_settings_format_supported(settings_format: int) -> bool:
    """Whether this build may write a record of this shape; a too-new record stays read-only."""
    return settings_format <= SUPPORTED_SETTINGS_FORMAT
