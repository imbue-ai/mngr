"""Minds application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the default account for new workspaces
and the auto-open behavior for the requests panel.

The env-selection URL (``connector_url``, ``litellm_proxy_url``) lives in
the per-tier ``ClientEnvConfig`` loaded via ``--config-file``; this file is
only for genuinely user-personal preferences and never carries tier state.
"""

import logging
import threading
from pathlib import Path
from typing import Final

import tomlkit
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.design_tokens import WorkspaceColor
from imbue.minds.desktop_client.design_tokens import oklch_starting_color
from imbue.minds.errors import MindsConfigError

_CONFIG_FILENAME: Final[str] = "config.toml"
_WORKSPACE_COLORS_KEY: Final[str] = "workspace_colors"

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


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

    def get_workspace_color(self, agent_id: str) -> WorkspaceColor:
        """Return the persisted workspace color for an agent.

        If no entry exists, materializes the deterministic OKLCH starting
        color (computed via SHA-256 over the agent id) into the config
        under the same lock and returns it. The agent creator can pre-empt
        this by calling :meth:`set_workspace_color` with an explicit preset
        before the first read; without that, every agent ever observed by
        minds ends up with a stable starting color from first read onward.

        On an unparseable stored value (corruption), logs a warning and
        falls back to the OKLCH starting color *without* overwriting the
        existing entry -- the user can see and correct the bad value rather
        than having it silently replaced.
        """
        with self._lock:
            data = self._read_raw()
            table = data.get(_WORKSPACE_COLORS_KEY)
            if isinstance(table, dict):
                stored = table.get(agent_id)
                if stored is not None:
                    try:
                        return WorkspaceColor(str(stored))
                    except ValueError:
                        _LOG.warning(
                            "Unparseable workspace color for %s in config: %r; "
                            "rendering with the OKLCH starting color and leaving "
                            "the stored value in place.",
                            agent_id,
                            stored,
                        )
                        return oklch_starting_color(agent_id)
            starting = oklch_starting_color(agent_id)
            self._write_workspace_color_locked(data, agent_id, starting)
            return starting

    def set_workspace_color(self, agent_id: str, color: WorkspaceColor) -> None:
        """Persist a workspace color for an agent.

        Stores the value as-is (preset slug or CSS literal). Validation
        happens at the :class:`WorkspaceColor` construction boundary, so
        callers can rely on stored values round-tripping cleanly.
        """
        with self._lock:
            data = self._read_raw()
            self._write_workspace_color_locked(data, agent_id, color)

    def remove_workspace_color(self, agent_id: str) -> None:
        """Drop the stored workspace color for an agent.

        Called from the destroy flow's success path to keep ``config.toml``
        tidy. Idempotent: missing entries are a no-op.
        """
        with self._lock:
            data = self._read_raw()
            table = data.get(_WORKSPACE_COLORS_KEY)
            if not isinstance(table, dict) or agent_id not in table:
                return
            del table[agent_id]
            if not table:
                del data[_WORKSPACE_COLORS_KEY]
            self._write_raw(data)

    def _write_workspace_color_locked(
        self,
        data: dict[str, object],
        agent_id: str,
        color: WorkspaceColor,
    ) -> None:
        """Write a single workspace color into ``data`` and flush.

        Must be called while holding ``self._lock``. Hoists the
        ``[workspace_colors]`` table if missing.
        """
        table = data.get(_WORKSPACE_COLORS_KEY)
        if not isinstance(table, dict):
            table = {}
            data[_WORKSPACE_COLORS_KEY] = table
        table[agent_id] = str(color)
        self._write_raw(data)
