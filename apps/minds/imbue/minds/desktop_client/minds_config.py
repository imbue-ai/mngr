"""Imbue Studio application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the error-reporting preferences and the
order in which accounts and workspaces were last shown.

The env-selection URL (``connector_url``, ``litellm_proxy_url``) lives in
the per-tier ``ClientEnvConfig`` loaded via ``--config-file``; this file is
only for genuinely user-personal preferences and never carries tier state.
"""

import threading
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Callable
from typing import Final
from typing import assert_never

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import StrictInt
from pydantic import model_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindsConfigError
from imbue.minds.errors import WorkspaceAutoUpdateSettingError

_CONFIG_FILENAME: Final[str] = "config.toml"

# How many accounts, and how many workspaces per account, the ``[recent]``
# lists keep. Both are consulted for a switch target and a new window's account,
# so a bound well above what one install holds loses nothing.
MAX_RECENT_ACCOUNTS: Final[int] = 32
MAX_RECENT_WORKSPACES_PER_ACCOUNT: Final[int] = 64

# Local-clock hours [start, end) scheduled updates run in by default.
DEFAULT_UPDATE_WINDOW: Final[tuple[int, int]] = (2, 5)


class NotificationStyle(LowerCaseStrEnum):
    """Delivery style for feed-backed notifications: in-app toast cards, OS notifications, or both.

    The lowercase values are the wire strings the settings API serves and
    accepts verbatim (and what ``config.toml`` stores).
    """

    CARDS = auto()
    OS = auto()
    BOTH = auto()


DEFAULT_NOTIFICATION_STYLE: Final[NotificationStyle] = NotificationStyle.BOTH


class AutoUpdatePromptDecision(LowerCaseStrEnum):
    """How the user answered the one-time offer to turn on automatic workspace updates.

    The lowercase values are the wire strings the settings API serves and accepts verbatim
    (and what ``config.toml`` stores).
    """

    ACCEPTED = auto()
    DECLINED = auto()


class WorkspaceAutoUpdateMode(LowerCaseStrEnum):
    """A workspace's own auto-update setting, in place of the app-wide one.

    The lowercase values are the wire strings the update routes accept (and what ``config.toml`` stores).
    """

    # Auto-update on for this workspace, in its own window, whatever the app-wide switch says.
    CUSTOM_WINDOW = auto()
    # Never auto-updated; updated only by hand.
    MANUAL = auto()


class WorkspaceAutoUpdateOverride(FrozenModel):
    """One workspace's own auto-update setting; a workspace without one follows the app-wide setting."""

    mode: WorkspaceAutoUpdateMode = Field(description="Auto-update in its own window, or never")
    window: tuple[StrictInt, StrictInt] | None = Field(
        default=None, description="Local-clock hours [start, end) for CUSTOM_WINDOW; None for MANUAL"
    )

    @model_validator(mode="after")
    def _window_matches_mode(self) -> "WorkspaceAutoUpdateOverride":
        match self.mode:
            case WorkspaceAutoUpdateMode.CUSTOM_WINDOW:
                if self.window is None or not is_usable_update_window(self.window[0], self.window[1]):
                    raise WorkspaceAutoUpdateSettingError(
                        f"A custom auto-update window needs two distinct hours in 0-23, got {self.window}"
                    )
            case WorkspaceAutoUpdateMode.MANUAL:
                if self.window is not None:
                    raise WorkspaceAutoUpdateSettingError("A manual auto-update setting has no window")
            case _ as unreachable:
                assert_never(unreachable)
        return self


class EffectiveAutoUpdate(FrozenModel):
    """What auto-update does for one workspace: its own setting when it has one, else the app's."""

    is_enabled: bool = Field(description="Whether the background check arms this workspace's update itself")
    window: tuple[int, int] = Field(description="Local-clock hours [start, end) this workspace's updates start in")
    override: WorkspaceAutoUpdateOverride | None = Field(description="The workspace's own setting; None to follow")


@pure
def is_usable_update_window(start_hour: object, end_hour: object) -> bool:
    """Whether two stored values make an update window: whole hours in 0-23 that differ."""
    # bool is an int subclass; a stored ``true`` is not hour one.
    if not isinstance(start_hour, int) or not isinstance(end_hour, int):
        return False
    if isinstance(start_hour, bool) or isinstance(end_hour, bool):
        return False
    return 0 <= start_hour <= 23 and 0 <= end_hour <= 23 and start_hour != end_hour


@pure
def effective_auto_update(
    app_is_enabled: bool, app_window: tuple[int, int], override: WorkspaceAutoUpdateOverride | None
) -> EffectiveAutoUpdate:
    """One workspace's auto-update: a custom window turns it on in its own hours, manual turns it off."""
    if override is None:
        return EffectiveAutoUpdate(is_enabled=app_is_enabled, window=app_window, override=None)
    match override.mode:
        case WorkspaceAutoUpdateMode.CUSTOM_WINDOW:
            assert override.window is not None
            return EffectiveAutoUpdate(is_enabled=True, window=override.window, override=override)
        case WorkspaceAutoUpdateMode.MANUAL:
            return EffectiveAutoUpdate(is_enabled=False, window=app_window, override=override)
        case _ as unreachable:
            assert_never(unreachable)


def _override_from_raw(value: object) -> WorkspaceAutoUpdateOverride | None:
    """A stored workspace setting, or None (logged) when it is missing or unusable."""
    table = _as_str_keyed_dict(value)
    if table is None:
        return None
    start = table.get("window_start_hour")
    end = table.get("window_end_hour")
    try:
        return WorkspaceAutoUpdateOverride.model_validate(
            {
                "mode": table.get("mode"),
                "window": (start, end) if start is not None or end is not None else None,
            }
        )
    except ValueError as e:
        logger.warning("Ignoring an unusable workspace auto-update setting {!r}: {}", table, e)
        return None


def _update_window_from_table(table: dict[str, object]) -> tuple[int, int]:
    """The window stored in ``table``'s ``window_*_hour`` keys, or the default when unusable."""
    start = table.get("window_start_hour")
    end = table.get("window_end_hour")
    if isinstance(start, int) and isinstance(end, int) and is_usable_update_window(start, end):
        return (start, end)
    if start is not None or end is not None:
        logger.warning("Ignoring an unusable update window ({}, {})", start, end)
    return DEFAULT_UPDATE_WINDOW


def _bool_from_raw(data: dict[str, object], key: str, default: bool) -> bool:
    """Read a top-level boolean out of an already-loaded config dict, or ``default`` when unset/malformed."""
    value = data.get(key)
    return value if isinstance(value, bool) else default


def _style_from_raw(data: dict[str, object]) -> NotificationStyle:
    """Read the notification style out of an already-loaded config dict, or the default when unset/malformed."""
    value = data.get("notification_style")
    if isinstance(value, str):
        try:
            return NotificationStyle(value)
        except ValueError:
            pass
    return DEFAULT_NOTIFICATION_STYLE


def _write_notification_prefs_into_raw(data: dict[str, object], is_enabled: bool, style: NotificationStyle) -> None:
    """Set the notification-prefs keys on an already-loaded config dict (the caller writes it)."""
    data["notifications_enabled"] = is_enabled
    data["notification_style"] = str(style)
    # CLEANUP: drop this pop once every install has written its prefs at least
    # once on a build without the browser-mode OS-permission hint (the key was
    # retired with that hint).
    data.pop("notification_os_hint_dismissed", None)


def _has_notification_choice(data: dict[str, object]) -> bool:
    """Existing saved preferences count as a choice, including on older installs."""
    return data.get("notification_style") in tuple(NotificationStyle) or isinstance(
        data.get("notifications_enabled"), bool
    )


def _as_str_keyed_dict(value: object) -> dict[str, object] | None:
    """Return ``value`` as a concretely-typed ``dict[str, object]``, or None if it isn't a mapping.

    The TOML loader yields dynamically-typed nested values, so a sub-table read
    out of the raw config is statically ``object``. Re-materializing it into a
    fresh ``dict[str, object]`` gives downstream code typed key/value access (and
    a private copy that's safe to mutate) without resorting to ``cast``.
    """
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _str_list(value: object) -> list[str]:
    """A TOML array read as a list of strings; anything else reads as empty."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _recent_account_ids(data: dict[str, object]) -> list[str]:
    recent = _as_str_keyed_dict(data.get("recent"))
    return _str_list(recent.get("accounts")) if recent is not None else []


def _recent_workspace_ids(data: dict[str, object], account_id: str) -> list[str]:
    recent = _as_str_keyed_dict(data.get("recent"))
    if recent is None:
        return []
    by_account = _as_str_keyed_dict(recent.get("workspaces_by_account"))
    return _str_list(by_account.get(account_id)) if by_account is not None else []


@pure
def _moved_to_front(items: Sequence[str], item: str, max_count: int) -> list[str]:
    """``items`` with ``item`` first (added when absent), bounded to ``max_count``."""
    return [item] + [other for other in items if other != item][: max_count - 1]


def _write_text_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` through a sibling temp file, creating the parent directory as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(text)
    tmp_path.rename(path)


class MindsConfig(MutableModel):
    """Thread-safe configuration manager for ``~/.minds/config.toml``."""

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _config_path(self) -> Path:
        return self.data_dir / _CONFIG_FILENAME

    def _read_raw(self) -> dict[str, object]:
        """Read the TOML config file.

        Returns an empty dict if the file does not exist (no config yet).
        Raises MindsConfigError if the file exists but cannot be read or
        parsed -- we refuse to silently fall back to defaults in that case
        because doing so would hide data corruption from the user.
        """
        path = self._config_path
        if not path.exists():
            return {}
        try:
            text = path.read_text()
        except OSError as e:
            raise MindsConfigError(f"Cannot read {path}: {e}") from e
        try:
            return dict(tomlkit.loads(text))
        except ValueError as e:
            raise MindsConfigError(f"Failed to parse {path}: {e}") from e

    def _write_raw(self, data: dict[str, object]) -> None:
        """Write the config data to TOML file atomically.

        Raises MindsConfigError when the file cannot be written, so a caller
        handles a failed write the way it handles a failed read.
        """
        path = self._config_path
        try:
            _write_text_atomically(path, tomlkit.dumps(data))
        except OSError as e:
            raise MindsConfigError(f"Cannot write {path}: {e}") from e

    def get_recent_account_ids(self) -> list[str]:
        """The accounts windows of this install last showed, most recent first (``[recent].accounts``)."""
        with self._lock:
            return _recent_account_ids(self._read_raw())

    def get_recent_workspace_ids(self, account_id: str) -> list[str]:
        """The workspaces windows on ``account_id`` last opened, most recent first."""
        with self._lock:
            return _recent_workspace_ids(self._read_raw(), account_id)

    def note_window_shows(self, account_id: str, workspace_id: str | None) -> bool:
        """Move ``account_id`` (and ``workspace_id`` within it) to the front of the recent lists.

        Fed by every window's ``client_state`` report, so it runs often: the file is
        rewritten only when the order it records changes. Returns whether it did.
        """
        with self._lock:
            data = self._read_raw()
            recent_accounts = _recent_account_ids(data)
            moved_accounts = _moved_to_front(recent_accounts, account_id, MAX_RECENT_ACCOUNTS)
            recent_workspaces = _recent_workspace_ids(data, account_id)
            moved_workspaces = (
                recent_workspaces
                if workspace_id is None
                else _moved_to_front(recent_workspaces, workspace_id, MAX_RECENT_WORKSPACES_PER_ACCOUNT)
            )
            if moved_accounts == recent_accounts and moved_workspaces == recent_workspaces:
                return False
            recent = _as_str_keyed_dict(data.get("recent")) or {}
            workspaces_by_account = _as_str_keyed_dict(recent.get("workspaces_by_account")) or {}
            workspaces_by_account[account_id] = moved_workspaces
            recent["accounts"] = moved_accounts
            recent["workspaces_by_account"] = workspaces_by_account
            data["recent"] = recent
            self._write_raw(data)
            return True

    def get_sign_in_browser_id(self) -> str | None:
        """Return the browser provider sign-ins open in (see ``sign_in_browser``), or None for the default."""
        with self._lock:
            data = self._read_raw()
            value = data.get("sign_in_browser")
            return str(value) if value is not None else None

    def set_sign_in_browser_id(self, browser_id: str | None) -> None:
        """Set the browser provider sign-ins open in, or None to go back to the default."""
        with self._lock:
            data = self._read_raw()
            if browser_id is not None:
                data["sign_in_browser"] = browser_id
            elif "sign_in_browser" in data:
                del data["sign_in_browser"]
            else:
                pass
            self._write_raw(data)

    def get_region(self, provider_name: str) -> str | None:
        """Return the last-used region for a provider, or None if never set.

        Stored under ``[providers.<provider_name>].region`` so each
        region-bearing provider (e.g. ``imbue_cloud``, ``vultr``) keeps its own
        last-used value. The create form defaults to this; on a successful
        create the chosen region is written back via :meth:`set_region`.
        """
        with self._lock:
            data = self._read_raw()
            providers = _as_str_keyed_dict(data.get("providers"))
            if providers is None:
                return None
            provider = _as_str_keyed_dict(providers.get(provider_name))
            if provider is None:
                return None
            value = provider.get("region")
            return str(value) if value is not None else None

    def set_region(self, provider_name: str, region: str) -> None:
        """Persist the last-used region for a provider under ``[providers.<provider_name>]``."""
        with self._lock:
            data = self._read_raw()
            providers = _as_str_keyed_dict(data.get("providers")) or {}
            provider = _as_str_keyed_dict(providers.get(provider_name)) or {}
            provider["region"] = region
            providers[provider_name] = provider
            data["providers"] = providers
            self._write_raw(data)

    def get_update_window(self) -> tuple[int, int]:
        """Return the local-clock hours [start, end) during which scheduled updates may run.

        An unusable stored pair falls back to the default: raising would silently
        stop every scheduled update with no surface to report it on.
        """
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates"))
            if updates is None:
                return DEFAULT_UPDATE_WINDOW
            return _update_window_from_table(updates)

    def set_update_window(self, start_hour: int, end_hour: int) -> None:
        """Persist the local-clock hours scheduled updates may run in."""
        self._write_updates_keys({"window_start_hour": start_hour, "window_end_hour": end_hour})

    def _read_updates_table(self) -> dict[str, object]:
        """The ``[updates]`` table, empty when absent. Must hold ``self._lock``."""
        return _as_str_keyed_dict(self._read_raw().get("updates")) or {}

    def _write_updates_keys(self, values: dict[str, object]) -> None:
        """Set keys of the ``[updates]`` table in one read-modify-write."""
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates")) or {}
            updates.update(values)
            data["updates"] = updates
            self._write_raw(data)

    def get_auto_update_workspaces(self) -> bool:
        """Return whether Imbue Studio arms every behind workspace without its own setting. Default: False."""
        with self._lock:
            return _bool_from_raw(self._read_updates_table(), "auto_update_workspaces", False)

    def set_auto_update_workspaces(self, is_enabled: bool) -> None:
        """Persist whether Imbue Studio arms every behind workspace without its own setting."""
        self._write_updates_keys({"auto_update_workspaces": is_enabled})

    def get_auto_update_prompt_decision(self) -> AutoUpdatePromptDecision | None:
        """Return how the user answered the auto-update offer, or None while it has never been answered."""
        with self._lock:
            value = self._read_updates_table().get("auto_update_prompt_decision")
        if not isinstance(value, str):
            return None
        try:
            return AutoUpdatePromptDecision(value)
        except ValueError:
            logger.warning("Ignoring an unknown auto-update prompt decision {!r}", value)
            return None

    def set_auto_update_prompt_decision(self, decision: AutoUpdatePromptDecision) -> None:
        """Record how the user answered the auto-update offer, so it is never shown again."""
        self._write_updates_keys({"auto_update_prompt_decision": str(decision)})

    def get_skipped_update_target(self, agent_id: str) -> str | None:
        """Return the release the user skipped for this workspace under auto-update, or None."""
        with self._lock:
            skipped = _as_str_keyed_dict(self._read_updates_table().get("skipped_targets")) or {}
        value = skipped.get(agent_id)
        return str(value) if isinstance(value, str) and value else None

    def set_skipped_update_target(self, agent_id: str, target_ref: str) -> None:
        """Record the release the user skipped for this workspace under auto-update."""
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates")) or {}
            skipped = _as_str_keyed_dict(updates.get("skipped_targets")) or {}
            skipped[agent_id] = target_ref
            updates["skipped_targets"] = skipped
            data["updates"] = updates
            self._write_raw(data)

    def get_workspace_auto_update_overrides(self) -> dict[str, WorkspaceAutoUpdateOverride]:
        """Every workspace's own auto-update setting, keyed by agent id; unusable entries are left out."""
        with self._lock:
            by_workspace = _as_str_keyed_dict(self._read_updates_table().get("workspace_settings")) or {}
        overrides = {agent_id: _override_from_raw(value) for agent_id, value in by_workspace.items()}
        return {agent_id: override for agent_id, override in overrides.items() if override is not None}

    def set_workspace_auto_update_override(self, agent_id: str, override: WorkspaceAutoUpdateOverride | None) -> None:
        """Persist this workspace's own auto-update setting; None puts it back on the app-wide one."""
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates")) or {}
            by_workspace = _as_str_keyed_dict(updates.get("workspace_settings")) or {}
            if override is None:
                if by_workspace.pop(agent_id, None) is None:
                    return
            else:
                entry: dict[str, object] = {"mode": str(override.mode)}
                if override.window is not None:
                    entry["window_start_hour"], entry["window_end_hour"] = override.window
                by_workspace[agent_id] = entry
            updates["workspace_settings"] = by_workspace
            data["updates"] = updates
            self._write_raw(data)

    def get_effective_auto_update(self, agent_id: str) -> EffectiveAutoUpdate:
        """What auto-update does for this workspace: its own setting when it has one, else the app's."""
        with self._lock:
            updates = self._read_updates_table()
        by_workspace = _as_str_keyed_dict(updates.get("workspace_settings")) or {}
        return effective_auto_update(
            app_is_enabled=_bool_from_raw(updates, "auto_update_workspaces", False),
            app_window=_update_window_from_table(updates),
            override=_override_from_raw(by_workspace.get(agent_id)),
        )

    def _get_bool(self, key: str, default: bool) -> bool:
        """Read a top-level boolean setting, returning ``default`` when unset or malformed."""
        with self._lock:
            data = self._read_raw()
            return _bool_from_raw(data, key, default)

    def _set_bool(self, key: str, value: bool) -> None:
        """Persist a top-level boolean setting."""
        with self._lock:
            data = self._read_raw()
            data[key] = value
            self._write_raw(data)

    def get_error_reporting_consent_given(self) -> bool:
        """Return whether the user has seen and answered the error-reporting consent screen. Default: False.

        Gates the first-launch consent screen: while False, the consent screen is shown ahead of
        the home page; once the user answers it (either way) this flips to True and stays there.
        """
        return self._get_bool("error_reporting_consent_given", default=False)

    def set_error_reporting_consent_given(self, given: bool) -> None:
        """Record that the user has answered the error-reporting consent screen."""
        self._set_bool("error_reporting_consent_given", given)

    def get_is_onboarding_complete(self) -> bool:
        """Return whether this installation has been taken past the first-run start flow. Default: False.

        Flips to True once the user starts creating a workspace (any surface) or signs in
        from the start flow's title-bar "Already set up? Log in"; while False and no workspace
        exists, a launch lands on the start flow.
        """
        return self._get_bool("is_onboarding_complete", default=False)

    def set_is_onboarding_complete(self, is_complete: bool) -> None:
        """Record whether the installation has been taken past the first-run start flow."""
        self._set_bool("is_onboarding_complete", is_complete)

    def get_is_browser_import_offered(self) -> bool:
        """Return whether the one-time offer to import the user's Chrome sign-ins has been shown. Default: False.

        The permission dialog makes the offer once, on the first Approve that is about
        to open a browser for a sign-in; after that the import lives in Settings only.
        """
        return self._get_bool("is_browser_import_offered", default=False)

    def set_is_browser_import_offered(self, is_offered: bool) -> None:
        """Record whether the one-time Chrome sign-in import offer has been shown."""
        self._set_bool("is_browser_import_offered", is_offered)

    def get_report_unexpected_errors(self) -> bool:
        """Return whether unexpected errors (with their log/traceback attachments) are reported to
        Sentry automatically. Default: True.

        A single flag gating both automatic error sends and whether their log/traceback attachments
        are uploaded. It defaults on for new installs; the consent question's checkbox (checked to
        start; asked in the start flow, or on the consent screen) and Settings -> Error reporting
        both turn it off.
        Read live at Sentry send time (so a change takes effect without an app restart). Manual bug
        reports are an explicit user action and are sent (with full diagnostics) regardless of this
        setting.
        """
        return self._get_bool("report_unexpected_errors", default=True)

    def set_report_unexpected_errors(self, enabled: bool) -> None:
        """Set whether unexpected errors are reported to Sentry automatically."""
        self._set_bool("report_unexpected_errors", enabled)

    def set_report_unexpected_errors_if_version_matches(
        self,
        expected_version: str,
        compute_version: Callable[[bool], str],
        enabled: bool,
    ) -> str | None:
        """Atomically compare-and-swap the error-reporting flag under one lock hold.

        Checks ``expected_version`` against the version ``compute_version``
        derives from the flag's CURRENT stored value, and only then writes --
        both the check and the write inside one lock acquisition. Unlike
        calling :meth:`get_report_unexpected_errors` and
        :meth:`set_report_unexpected_errors` separately (two lock
        acquisitions with a gap between them), this closes the window where
        two concurrent writers starting from the same version could both pass
        the check and the second would silently clobber the first with no
        conflict reported to either. Returns the new version, or None on a
        version mismatch (the caller should treat this as a conflict).
        """
        with self._lock:
            data = self._read_raw()
            current_enabled = _bool_from_raw(data, "report_unexpected_errors", True)
            if compute_version(current_enabled) != expected_version:
                return None
            data["report_unexpected_errors"] = enabled
            self._write_raw(data)
            return compute_version(enabled)

    def get_notification_prefs(self) -> tuple[bool, NotificationStyle]:
        """Return ``(is_enabled, style)`` from one atomic read.

        Reading the fields via separate locked calls could observe a
        concurrent writer's update to only one of them -- a combination that
        :meth:`set_notification_prefs` never actually persisted together. One
        lock acquisition here mirrors that write's atomicity on the read side.
        """
        is_enabled, style, _has_chosen = self.get_notification_prefs_with_choice()
        return is_enabled, style

    def get_notification_prefs_with_choice(self) -> tuple[bool, NotificationStyle, bool]:
        """Read the preferences and whether the user has chosen them in one snapshot."""
        with self._lock:
            data = self._read_raw()
            return (
                _bool_from_raw(data, "notifications_enabled", True),
                _style_from_raw(data),
                _has_notification_choice(data),
            )

    def set_notification_prefs(
        self,
        is_enabled: bool,
        style: NotificationStyle,
    ) -> None:
        """Persist both notification preferences in one read-modify-write.

        A single lock acquisition and a single atomic file write, so two
        concurrent writers can never interleave into a record that mixes one
        writer's toggle with the other's style.
        """
        with self._lock:
            data = self._read_raw()
            _write_notification_prefs_into_raw(data, is_enabled, style)
            self._write_raw(data)

    def set_notification_prefs_if_version_matches(
        self,
        expected_version: str,
        compute_version: Callable[[bool, NotificationStyle, bool], str],
        is_enabled: bool,
        style: NotificationStyle,
    ) -> str | None:
        """Atomically compare-and-swap the notification-prefs record under one lock hold.

        Checks ``expected_version`` against the version ``compute_version``
        derives from the record's CURRENT stored values, and only then
        writes -- both the check and the write inside one lock acquisition.
        Unlike calling :meth:`get_notification_prefs` and
        :meth:`set_notification_prefs` separately (two lock acquisitions
        with a gap between them), this closes the window where two
        concurrent writers starting from the same version could both pass
        the check and the second would silently clobber the first with no
        conflict reported to either. Returns the new version, or None on a
        version mismatch (the caller should treat this as a conflict).
        """
        with self._lock:
            data = self._read_raw()
            current_is_enabled = _bool_from_raw(data, "notifications_enabled", True)
            current_style = _style_from_raw(data)
            if compute_version(current_is_enabled, current_style, _has_notification_choice(data)) != expected_version:
                return None
            _write_notification_prefs_into_raw(data, is_enabled, style)
            self._write_raw(data)
            return compute_version(is_enabled, style, True)
