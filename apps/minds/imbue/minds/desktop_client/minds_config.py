"""Minds application configuration stored in ``~/.minds/config.toml``.

Provides a thread-safe interface for reading and writing user preferences
that persist across sessions, such as the default account for new workspaces
and the auto-open behavior for the requests panel.

Also exposes static service URLs (``cloudflare_forwarding_url``,
``supertokens_connection_uri``) used by the desktop client to talk to the
backing services. Those URLs follow env > file > default precedence so ops
can point a local build at a different deployment without editing code.
"""

import os
import threading
from pathlib import Path
from typing import Final

import tomlkit
from pydantic import AnyUrl
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindsConfigError

_CONFIG_FILENAME = "config.toml"

DEFAULT_CLOUDFLARE_FORWARDING_URL: Final[str] = "https://joshalbrecht--cloudflare-forwarding-fastapi-app.modal.run"
DEFAULT_SUPERTOKENS_CONNECTION_URI: Final[str] = (
    "https://st-dev-aba73a80-3754-11f1-9afe-f5bb4fa720bc.aws.supertokens.io"
)

_CLOUDFLARE_FORWARDING_URL_ENV: Final[str] = "CLOUDFLARE_FORWARDING_URL"
_SUPERTOKENS_CONNECTION_URI_ENV: Final[str] = "SUPERTOKENS_CONNECTION_URI"

_CLOUDFLARE_FORWARDING_URL_KEY: Final[str] = "cloudflare_forwarding_url"
_SUPERTOKENS_CONNECTION_URI_KEY: Final[str] = "supertokens_connection_uri"

_URL_VALIDATOR: Final[TypeAdapter[AnyUrl]] = TypeAdapter(AnyUrl)


def _validate_url(raw: str, source: str) -> AnyUrl:
    try:
        return _URL_VALIDATOR.validate_python(raw)
    except ValidationError as e:
        raise MindsConfigError(f"Invalid URL in {source}: {raw!r}") from e


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

    def _resolve_url_setting(
        self,
        *,
        env_var: str,
        file_key: str,
        default: str,
    ) -> AnyUrl:
        """Resolve a URL setting with precedence env > file > default.

        Raises MindsConfigError if the env or file value is not a valid URL.
        The default is assumed well-formed (validated at import time in tests).
        """
        env_value = os.environ.get(env_var)
        if env_value is not None:
            return _validate_url(env_value, source=f"${env_var}")
        with self._lock:
            data = self._read_raw()
        file_value = data.get(file_key)
        if isinstance(file_value, str):
            return _validate_url(file_value, source=f"{self._config_path}:{file_key}")
        return _validate_url(default, source=f"{file_key} default")

    @property
    def cloudflare_forwarding_url(self) -> AnyUrl:
        """Base URL of the Cloudflare forwarding API.

        Precedence: ``$CLOUDFLARE_FORWARDING_URL`` > ``config.toml`` > built-in default.
        """
        return self._resolve_url_setting(
            env_var=_CLOUDFLARE_FORWARDING_URL_ENV,
            file_key=_CLOUDFLARE_FORWARDING_URL_KEY,
            default=DEFAULT_CLOUDFLARE_FORWARDING_URL,
        )

    @property
    def supertokens_connection_uri(self) -> AnyUrl:
        """URI of the SuperTokens core.

        Precedence: ``$SUPERTOKENS_CONNECTION_URI`` > ``config.toml`` > built-in default.
        """
        return self._resolve_url_setting(
            env_var=_SUPERTOKENS_CONNECTION_URI_ENV,
            file_key=_SUPERTOKENS_CONNECTION_URI_KEY,
            default=DEFAULT_SUPERTOKENS_CONNECTION_URI,
        )
