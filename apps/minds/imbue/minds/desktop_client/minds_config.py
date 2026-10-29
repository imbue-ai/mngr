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

import tomlkit
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindsConfigError

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


def _write_notification_style_into_raw(data: dict[str, object], style: NotificationStyle) -> None:
    """Set the notification-style key on an already-loaded config dict (the caller writes it)."""
    data["notification_style"] = str(style)
    # CLEANUP: drop this pop once every install has written its prefs at least
    # once on a build without the browser-mode OS-permission hint (the key was
    # retired with that hint).
    data.pop("notification_os_hint_dismissed", None)


def _has_notification_choice(data: dict[str, object]) -> bool:
    """A saved style counts as a choice; so does a master toggle an older install stored here.

    The toggle moved to the per-account settings record, whose own choice the
    caller ORs in; the key check stays for a config the migration has not
    visited (it never writes the key back, so this only ever reads old state).
    """
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

    def _read_updates_table(self) -> dict[str, object]:
        """The ``[updates]`` table, empty when absent. Must hold ``self._lock``."""
        return _as_str_keyed_dict(self._read_raw().get("updates")) or {}

    def read_account_keyed_settings_for_migration(self) -> dict[str, object]:
        """The account-keyed values ``config.toml`` still holds, keyed by their settings-record names.

        Only explicitly-stored, usable values are returned: absence means the
        user never chose, which the settings record expresses by leaving the
        key out. Empty once the migration has deleted the moved keys.
        """
        # CLEANUP: remove with settings/migration.py once every supported install has started a phase-3A build.
        with self._lock:
            data = self._read_raw()
        values: dict[str, object] = {}
        updates = _as_str_keyed_dict(data.get("updates")) or {}
        start = updates.get("window_start_hour")
        end = updates.get("window_end_hour")
        # bool is an int subclass; a stored ``true`` is not hour one.
        is_usable_window = (
            isinstance(start, int)
            and isinstance(end, int)
            and not isinstance(start, bool)
            and not isinstance(end, bool)
            and 0 <= start <= 23
            and 0 <= end <= 23
            and start != end
        )
        if is_usable_window:
            values["update_window_start_hour"] = start
            values["update_window_end_hour"] = end
        is_auto_update = updates.get("auto_update_workspaces")
        if isinstance(is_auto_update, bool):
            values["auto_update_workspaces"] = is_auto_update
        decision = updates.get("auto_update_prompt_decision")
        if isinstance(decision, str) and decision in tuple(AutoUpdatePromptDecision):
            values["auto_update_prompt_decision"] = decision
        is_enabled = data.get("notifications_enabled")
        if isinstance(is_enabled, bool):
            values["notifications_enabled"] = is_enabled
        return values

    def delete_account_keyed_settings_for_migration(self) -> None:
        """Remove the moved account-keyed keys; the per-workspace skipped targets stay until their own phase."""
        # CLEANUP: remove with settings/migration.py once every supported install has started a phase-3A build.
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates")) or {}
            for key in (
                "window_start_hour",
                "window_end_hour",
                "auto_update_workspaces",
                "auto_update_prompt_decision",
            ):
                updates.pop(key, None)
            if updates:
                data["updates"] = updates
            else:
                data.pop("updates", None)
            data.pop("notifications_enabled", None)
            self._write_raw(data)

    def read_skipped_update_targets_for_migration(self) -> dict[str, str]:
        """The per-workspace skipped targets config.toml still holds, for the one-shot move into each workspace."""
        # CLEANUP: remove with workspace_facts_migration.py once every supported install has
        # started a phase-3B build.
        with self._lock:
            skipped = _as_str_keyed_dict(self._read_updates_table().get("skipped_targets")) or {}
        return {agent_id: str(value) for agent_id, value in skipped.items() if isinstance(value, str) and value}

    def delete_skipped_update_targets_for_migration(self) -> None:
        """Remove the moved skipped-target keys (their values now live in the facts cache and each workspace)."""
        # CLEANUP: remove with workspace_facts_migration.py once every supported install has
        # started a phase-3B build.
        with self._lock:
            data = self._read_raw()
            updates = _as_str_keyed_dict(data.get("updates")) or {}
            updates.pop("skipped_targets", None)
            if updates:
                data["updates"] = updates
            else:
                data.pop("updates", None)
            self._write_raw(data)

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
        from the start flow's "I have an account (log in)" answer; while False and no workspace
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

    def get_notification_style_with_choice(self) -> tuple[NotificationStyle, bool]:
        """Read the delivery style and whether this file records a notification choice, in one snapshot.

        The master toggle lives in the per-account settings record; a choice
        made only there is the caller's to OR in.
        """
        with self._lock:
            data = self._read_raw()
            return (_style_from_raw(data), _has_notification_choice(data))

    def set_notification_style_if_version_matches(
        self,
        expected_version: str,
        compute_version: Callable[[NotificationStyle, bool], str],
        style: NotificationStyle,
        # Whether a notification choice exists outside this file (the
        # account settings records): the served version was computed with it
        # ORed in, so the check here must be too.
        is_chosen_elsewhere: bool,
    ) -> str | None:
        """Atomically compare-and-swap the notification style under one lock hold.

        Checks ``expected_version`` against the version ``compute_version``
        derives from the style's CURRENT stored value, and only then
        writes -- both the check and the write inside one lock acquisition.
        Separate locked get and set calls would leave a window where two
        concurrent writers starting from the same version could both pass
        the check and the second would silently clobber the first with no
        conflict reported to either. Returns the new version, or None on a
        version mismatch (the caller should treat this as a conflict).
        """
        with self._lock:
            data = self._read_raw()
            current_style = _style_from_raw(data)
            has_chosen = _has_notification_choice(data) or is_chosen_elsewhere
            if compute_version(current_style, has_chosen) != expected_version:
                return None
            _write_notification_style_into_raw(data, style)
            self._write_raw(data)
            return compute_version(style, True)
