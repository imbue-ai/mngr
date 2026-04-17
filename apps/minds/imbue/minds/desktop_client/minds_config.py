"""Minds application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the default account for new workspaces
and the auto-open behavior for the requests panel.
"""

import threading
from pathlib import Path

import tomlkit
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

_CONFIG_FILENAME = "config.toml"


class MindsConfig(MutableModel):
    """Thread-safe configuration manager for ``~/.minds/config.toml``."""

    data_dir: Path = Field(frozen=True, description="Root data directory (e.g. ~/.minds)")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def _config_path(self) -> Path:
        return self.data_dir / _CONFIG_FILENAME

    def _read_raw(self) -> dict[str, object]:
        """Read the TOML config file. Returns empty dict if missing or corrupt."""
        path = self._config_path
        if not path.exists():
            return {}
        try:
            return dict(tomlkit.loads(path.read_text()))
        except (OSError, ValueError) as e:
            logger.warning("Failed to read config.toml: {}", e)
            return {}

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

    def get_auto_open_requests_panel(self) -> bool:
        """Return whether the requests panel should auto-open on new requests. Default: True."""
        with self._lock:
            data = self._read_raw()
            value = data.get("auto_open_requests_panel")
            if isinstance(value, bool):
                return value
            return True

    def set_auto_open_requests_panel(self, enabled: bool) -> None:
        """Set whether the requests panel should auto-open on new requests."""
        with self._lock:
            data = self._read_raw()
            data["auto_open_requests_panel"] = enabled
            self._write_raw(data)
