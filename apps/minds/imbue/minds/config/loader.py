"""Load per-tier and per-dev-env config files.

Per-env on-disk layout (see ``apps/minds/imbue/minds/envs/paths.py``
and the per-env-data-roots spec):

* Dev envs: ``~/.minds-<env-name>/client.toml`` -- non-secret config
  written by ``minds env deploy``. Read via ``load_client_config(path)``
  with the path coming from ``MINDS_CLIENT_CONFIG_PATH`` (or
  ``--config-file``); ``minds env activate`` sets the env var to this
  path.
* Staging / production: ``apps/minds/imbue/minds/config/envs/<tier>/client.toml``
  is committed to the repo and read directly via
  :func:`repo_tier_client_config_path`. ``minds env activate``
  points ``MINDS_CLIENT_CONFIG_PATH`` at the in-repo path; the deploy
  writer for these tiers never touches disk-local files (the values are
  computable from the tier's Modal workspace + app names).

There is no implicit fallback: ``minds run`` refuses to start unless
``--config-file`` is passed or ``MINDS_CLIENT_CONFIG_PATH`` is
exported. The bundled-Electron entry path always passes ``--config-file``
explicitly (the build embeds the file's path via
``MINDS_CLIENT_CONFIG_BUNDLE``).
"""

import tomllib
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.errors import MindError

_ENVS_DIR: Final[Path] = Path(__file__).parent / "envs"
_BUNDLED_DIR: Final[Path] = _ENVS_DIR / "_bundled"
_CLIENT_FILENAME: Final[str] = "client.toml"
_DEPLOY_FILENAME: Final[str] = "deploy.toml"


class EnvConfigError(MindError):
    """Raised when a per-tier or per-dev-env config file cannot be loaded."""


def repo_tier_client_config_path(tier: str) -> Path:
    """Return the in-repo ``apps/minds/imbue/minds/config/envs/<tier>/client.toml``.

    The path is returned even when the file does not exist on disk --
    callers that need existence check via ``.is_file()`` so the error
    message can be tier-specific. Only the ``staging`` / ``production``
    tiers commit a ``client.toml`` here; ``dev`` has no shared
    ``client.toml`` (per-dev envs each carry their own URLs).
    """
    return _ENVS_DIR / tier / _CLIENT_FILENAME


def bundled_client_config_path_or_none() -> Path | None:
    """Return the bundled ``_bundled/client.toml`` if it exists, else None.

    Populated at Electron build time by ``apps/minds/scripts/build.js``
    from ``MINDS_CLIENT_CONFIG_BUNDLE=<path>``. Used by the bundled
    Electron startup path to know what to pass as ``--config-file``
    when launching the backend.
    """
    bundled = _BUNDLED_DIR / _CLIENT_FILENAME
    if bundled.is_file():
        return bundled
    return None


def load_client_config(path: Path) -> ClientEnvConfig:
    """Parse a client config TOML file into a :class:`ClientEnvConfig`."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise EnvConfigError(f"Cannot read client config {path}: {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EnvConfigError(f"Failed to parse client config {path}: {exc}") from exc
    try:
        return ClientEnvConfig.model_validate(raw)
    except ValidationError as exc:
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
    except tomllib.TOMLDecodeError as exc:
        raise EnvConfigError(f"Failed to parse deploy config {path}: {exc}") from exc
    try:
        return DeployEnvConfig.model_validate(raw)
    except ValidationError as exc:
        raise EnvConfigError(f"Invalid deploy config at {path}: {exc}") from exc
