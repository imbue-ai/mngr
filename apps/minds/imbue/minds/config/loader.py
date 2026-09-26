"""Load per-tier and per-dev-env config files.

Per-env on-disk layout (see ``apps/minds/imbue/minds/envs/paths.py``
and the per-env-data-roots spec):

* Dev envs: ``~/.minds-<env-name>/client.toml`` -- non-secret config
  written by ``minds-admin env deploy``. Read via ``load_client_config(path)``
  with the path coming from ``MINDS_CLIENT_CONFIG_PATH`` (or
  ``--config-file``); ``minds-admin env activate`` sets the env var to this
  path.
* Staging / production: ``apps/minds/imbue/minds/config/envs/<tier>/client.toml``
  is committed to the repo and read directly via
  :func:`repo_tier_client_config_path`. ``minds-admin env activate``
  points ``MINDS_CLIENT_CONFIG_PATH`` at the in-repo path; the deploy
  writer for these tiers never touches disk-local files (the values are
  computable from the tier's Modal workspace + app names).

Production is the default: when neither ``--config-file`` nor
``MINDS_CLIENT_CONFIG_PATH`` is set and no other env is active
(``MINDS_ROOT_NAME`` unset or ``minds``), ``minds run`` loads the in-repo
production ``client.toml`` -- the same file a packaged build embeds -- so a
source checkout runs against production with nothing exported. Only a shell
that names a non-production env without saying where its config lives is
refused (see :func:`resolve_client_config_path`). The bundled-Electron entry
path always passes ``--config-file`` explicitly (the build embeds the file's
path via ``MINDS_CLIENT_CONFIG_BUNDLE``).
"""

import tomllib
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.bootstrap import resolve_minds_root_name
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.data_types import management_overlay_for_tier
from imbue.minds.errors import MindError

_ENVS_DIR: Final[Path] = Path(__file__).parent / "envs"
_BUNDLED_DIR: Final[Path] = _ENVS_DIR / "_bundled"
_CLIENT_FILENAME: Final[str] = "client.toml"
_DEPLOY_FILENAME: Final[str] = "deploy.toml"
_PRODUCTION_TIER: Final[str] = "production"


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


def resolve_client_config_path(explicit_config_file: Path | None) -> Path:
    """Return the client config ``minds run`` should load.

    An explicit path (``--config-file``, which click already fills from
    ``MINDS_CLIENT_CONFIG_PATH``) always wins. Without one, production is the
    default -- the in-repo production ``client.toml`` -- provided no other env
    is active: ``MINDS_ROOT_NAME`` unset or ``minds`` (production) resolves
    the same data root, so it is the only value under which silently picking
    production cannot point a shell at a different tier's data.

    Raises ``EnvConfigError`` when ``MINDS_ROOT_NAME`` names another env and
    nothing says where that env's config lives -- a half-activated shell,
    where defaulting to production would pair one env's data root with
    another's services. A value that is not a legal root name at all raises
    ``BootstrapError`` from :func:`resolve_minds_root_name`.
    """
    if explicit_config_file is not None:
        return explicit_config_file
    root_name = resolve_minds_root_name()
    if root_name != DEFAULT_MINDS_ROOT_NAME:
        raise EnvConfigError(
            f"{MINDS_ROOT_NAME_ENV_VAR}={root_name!r} names a non-production env but no client "
            "config path is set. Export MINDS_CLIENT_CONFIG_PATH (or pass --config-file) for that "
            f"env, or `unset {MINDS_ROOT_NAME_ENV_VAR}` to run against production."
        )
    production_config = repo_tier_client_config_path(_PRODUCTION_TIER)
    if not production_config.is_file():
        raise EnvConfigError(
            f"Production client config not found at {production_config}. This file is committed "
            "with the repo; check your checkout."
        )
    return production_config


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


def committed_deploy_config_tiers() -> list[str]:
    """The tiers with a committed ``imbue/minds/config/envs/<tier>/deploy.toml``, sorted by name."""
    return sorted(path.parent.name for path in _ENVS_DIR.glob(f"*/{_DEPLOY_FILENAME}"))


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
        config = DeployEnvConfig.model_validate(raw)
    except ValidationError as exc:
        raise EnvConfigError(f"Invalid deploy config at {path}: {exc}") from exc
    if config.management_plane is not None:
        _assert_operators_inside_tier_operator_block(config.management_plane, tier, path)
    return config


def _assert_operators_inside_tier_operator_block(config: ManagementPlaneConfig, tier: str, path: Path) -> None:
    """Every operator address must be a host in the tier's reserved operator block.

    Validated here rather than in the model: the operator block is the first
    /24 of the TIER's overlay allocation, and only the loader knows the tier.
    Boxes are assigned above the block at prep, so an address outside it would
    eventually collide with a box's.
    """
    operator_block = management_overlay_for_tier(tier).operator_block
    for operator in config.wireguard.operators:
        is_usable_host = operator.address in operator_block and operator.address not in (
            operator_block.network_address,
            operator_block.broadcast_address,
        )
        if not is_usable_host:
            raise EnvConfigError(
                f"Invalid deploy config at {path}: [management_plane] operator '{operator.name}' address "
                f"{operator.address} must be a host inside the reserved operator block {operator_block} "
                f"of tier '{tier}' (boxes are assigned above it at prep)"
            )


# Services that need a per-env Modal Secret backed by a Vault entry.
# Each name corresponds to an entry under ``.minds/template/<name>.sh``
# and produces a Modal Secret named ``<name>-<tier>-<deploy_id>`` via the
# deploy's ``build_per_env_secret_values``. The ``litellm-connector`` Modal
# Secret is NOT in this list -- it's a code-driven secret (no Vault entry
# exists or is expected) pushed by the deploy directly. Lives here (rather
# than in the operator-only deploy machinery) because every tier's committed
# ``deploy.toml`` must declare exactly this set in ``[secrets].services`` --
# the config tests pin that invariant.
_PER_ENV_SECRET_SERVICES: Final[tuple[str, ...]] = (
    "litellm",
    "supertokens",
    "cloudflare",
    "neon",
    "sharing",
    "storage",
    "sentry",
    "ssh-ca",
)


def per_env_secret_services() -> tuple[str, ...]:
    """Public accessor for the list of services that need per-env Modal Secrets."""
    return _PER_ENV_SECRET_SERVICES
