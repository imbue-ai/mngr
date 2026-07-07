"""Minds application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the default account for new workspaces
and the auto-open behavior for the inbox modal.

The env-selection URL (``connector_url``, ``litellm_proxy_url``) lives in
the per-tier ``ClientEnvConfig`` loaded via ``--config-file``; this file is
only for genuinely user-personal preferences and never carries tier state.
"""

import threading
from pathlib import Path
from typing import Final

import tomlkit
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindsConfigError

_CONFIG_FILENAME: Final[str] = "config.toml"


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
            value = data.get(key)
            if isinstance(value, bool):
                return value
            return default

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
        """Return whether unexpected errors are reported to Sentry automatically. Default: False.

        Read live at Sentry send time (so toggling it takes effect without an app restart).
        Manual bug reports are an explicit user action and are sent regardless of this setting.
        """
        return self._get_bool("report_unexpected_errors", default=False)

    def set_report_unexpected_errors(self, enabled: bool) -> None:
        """Set whether unexpected errors are reported to Sentry automatically."""
        self._set_bool("report_unexpected_errors", enabled)

    def get_include_error_logs(self) -> bool:
        """Return whether log/traceback attachments are included with error reports. Default: False.

        Read live when attachments are collected. Only meaningful in production/staging, where the
        S3 attachment bucket exists; development never uploads attachments regardless.
        """
        return self._get_bool("include_error_logs", default=False)

    def set_include_error_logs(self, enabled: bool) -> None:
        """Set whether log/traceback attachments are included with error reports."""
        self._set_bool("include_error_logs", enabled)

    def get_auto_open_requests_panel(self) -> bool:
        """Return whether the inbox should auto-open on new pending requests. Default: True.

        Setting key kept as ``auto_open_requests_panel`` for backward
        compatibility with existing on-disk configs; "panel" now refers
        to the inbox modal (the old side panel has been removed).
        """
        with self._lock:
            data = self._read_raw()
            value = data.get("auto_open_requests_panel")
            if isinstance(value, bool):
                return value
            return True

    def set_auto_open_requests_panel(self, enabled: bool) -> None:
        """Set whether the inbox should auto-open on new pending requests.

        Setting key kept as ``auto_open_requests_panel`` for backward
        compatibility; "panel" now refers to the inbox modal.
        """
        with self._lock:
            data = self._read_raw()
            data["auto_open_requests_panel"] = enabled
            self._write_raw(data)
