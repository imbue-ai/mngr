"""Minds application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the default account for new workspaces
and the error-reporting preferences.

The env-selection URL (``connector_url``, ``litellm_proxy_url``) lives in
the per-tier ``ClientEnvConfig`` loaded via ``--config-file``; this file is
only for genuinely user-personal preferences and never carries tier state.
"""

import threading
from enum import auto
from pathlib import Path
from typing import Callable
from typing import Final

import tomlkit
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindsConfigError

_CONFIG_FILENAME: Final[str] = "config.toml"


class NotificationStyle(LowerCaseStrEnum):
    """Delivery style for feed-backed notifications: in-app toast cards, OS notifications, or both.

    The lowercase values are the wire strings the settings API serves and
    accepts verbatim (and what ``config.toml`` stores).
    """

    CARDS = auto()
    OS = auto()
    BOTH = auto()


DEFAULT_NOTIFICATION_STYLE: Final[NotificationStyle] = NotificationStyle.BOTH


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
        """Write the config data to TOML file atomically."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._config_path
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(tomlkit.dumps(data))
        tmp_path.rename(path)

    def get_default_account_id(self) -> str | None:
        """Return the default account user ID for new workspaces, or None."""
        with self._lock:
            data = self._read_raw()
            value = data.get("default_account_id")
            return str(value) if value is not None else None

    def set_default_account_id(self, user_id: str | None) -> None:
        """Set or clear the default account for new workspaces."""
        with self._lock:
            data = self._read_raw()
            if user_id is not None:
                data["default_account_id"] = user_id
            elif "default_account_id" in data:
                del data["default_account_id"]
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
        welcome/login; once the user answers it (either way) this flips to True and stays there.
        """
        return self._get_bool("error_reporting_consent_given", default=False)

    def set_error_reporting_consent_given(self, given: bool) -> None:
        """Record that the user has answered the error-reporting consent screen."""
        self._set_bool("error_reporting_consent_given", given)

    def get_report_unexpected_errors(self) -> bool:
        """Return whether unexpected errors (with their log/traceback attachments) are reported to
        Sentry automatically. Default: True.

        A single flag gating both automatic error sends and whether their log/traceback attachments
        are uploaded. It defaults on for new installs (the first-launch consent screen is
        informational, with no opt-out there) but can be turned off from Settings -> Error reporting.
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

    def get_notifications_enabled(self) -> bool:
        """Return whether notification nudges are enabled at all. Default: True.

        The master switch for every OS notification the app sends (feed-backed
        request nudges, agent-sent notifications, backup failures). Read live
        at dispatch time so a Settings change applies without an app restart.
        The feed itself always records entries regardless of this switch.
        """
        return self._get_bool("notifications_enabled", default=True)

    def get_notification_prefs(self) -> tuple[bool, NotificationStyle, bool]:
        """Return ``(is_enabled, style, is_os_hint_dismissed)`` from one atomic read.

        Reading the fields via separate locked calls (as each getter does on
        its own) could observe a concurrent writer's update to only some of
        them -- a combination that :meth:`set_notification_prefs` never
        actually persisted together. One lock acquisition here mirrors that
        write's atomicity on the read side.
        """
        with self._lock:
            data = self._read_raw()
            return (
                _bool_from_raw(data, "notifications_enabled", True),
                _style_from_raw(data),
                _bool_from_raw(data, "notification_os_hint_dismissed", False),
            )

    def set_notification_prefs(
        self,
        is_enabled: bool,
        style: NotificationStyle,
        is_os_hint_dismissed: bool,
    ) -> None:
        """Persist all three notification preferences in one read-modify-write.

        A single lock acquisition and a single atomic file write, so two
        concurrent writers can never interleave into a record that mixes one
        writer's toggle with the other's style.
        """
        with self._lock:
            data = self._read_raw()
            data["notifications_enabled"] = is_enabled
            data["notification_style"] = str(style)
            data["notification_os_hint_dismissed"] = is_os_hint_dismissed
            self._write_raw(data)

    def set_notification_prefs_if_version_matches(
        self,
        expected_version: str,
        compute_version: Callable[[bool, NotificationStyle, bool], str],
        is_enabled: bool,
        style: NotificationStyle,
        is_os_hint_dismissed: bool,
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
            current_is_os_hint_dismissed = _bool_from_raw(data, "notification_os_hint_dismissed", False)
            if compute_version(current_is_enabled, current_style, current_is_os_hint_dismissed) != expected_version:
                return None
            data["notifications_enabled"] = is_enabled
            data["notification_style"] = str(style)
            data["notification_os_hint_dismissed"] = is_os_hint_dismissed
            self._write_raw(data)
            return compute_version(is_enabled, style, is_os_hint_dismissed)
