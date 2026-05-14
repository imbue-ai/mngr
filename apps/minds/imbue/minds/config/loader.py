"""Load per-tier and per-dev-env config files.

The ``--config-file <path>`` flag on ``minds run`` is the only env-selection
knob. Resolution for the default when ``--config-file`` is unset:

1. If ``imbue/minds/config/envs/_bundled/client.toml`` exists (written by
   the Electron production build step), return that path.
2. Otherwise fall back to ``imbue/minds/config/envs/dev/client.toml``.

The fall-through is what ``uv run minds run`` and any non-production build
see; release builds always write the production tier's ``client.toml`` into
``_bundled/`` before packaging the wheel.

Both paths resolve via ``Path(__file__).parent / "envs" / ...`` so the
function works identically when running from a source checkout and from an
installed wheel -- hatch ships the ``envs/`` directory verbatim.
"""

import tomllib
from pathlib import Path
from typing import Final

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.errors import MindError

_ENVS_DIR: Final[Path] = Path(__file__).parent / "envs"
_BUNDLED_DIR: Final[Path] = _ENVS_DIR / "_bundled"
_DEV_DIR: Final[Path] = _ENVS_DIR / "dev"
_CLIENT_FILENAME: Final[str] = "client.toml"
_DEPLOY_FILENAME: Final[str] = "deploy.toml"


class EnvConfigError(MindError):
    """Raised when a per-tier or per-dev-env config file cannot be loaded."""


def resolve_default_client_config_path() -> Path:
    """Return the path to the default client config.

    Honors the build-bundled file when present, otherwise returns the
    dev-tier file. Raises if neither is present (which should never happen
    in a normal install, since the dev fallback ships with the wheel).
    """
    bundled = _BUNDLED_DIR / _CLIENT_FILENAME
    if bundled.is_file():
        return bundled
    fallback = _DEV_DIR / _CLIENT_FILENAME
    if fallback.is_file():
        return fallback
    raise EnvConfigError(f"Could not locate default client config: neither {bundled} nor {fallback} exists.")


def load_client_config(path: Path) -> ClientEnvConfig:
    """Parse a client config TOML file into a :class:`ClientEnvConfig`."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise EnvConfigError(f"Cannot read client config {path}: {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except ValueError as exc:
        raise EnvConfigError(f"Failed to parse client config {path}: {exc}") from exc
    try:
        return ClientEnvConfig.model_validate(raw)
    except ValueError as exc:
        raise EnvConfigError(f"Invalid client config at {path}: {exc}") from exc


def load_deploy_config(tier: str) -> DeployEnvConfig:
    """Load a tier's deploy config from ``imbue/minds/config/envs/<tier>/deploy.toml``."""
    path = _ENVS_DIR / tier / _DEPLOY_FILENAME
    if not path.is_file():
        raise EnvConfigError(f"No deploy config found for tier {tier!r}: expected {path}")
    try:
        text = path.read_text()
    except OSError as exc:
        raise EnvConfigError(f"Cannot read deploy config {path}: {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except ValueError as exc:
        raise EnvConfigError(f"Failed to parse deploy config {path}: {exc}") from exc
    try:
        return DeployEnvConfig.model_validate(raw)
    except ValueError as exc:
        raise EnvConfigError(f"Invalid deploy config at {path}: {exc}") from exc
