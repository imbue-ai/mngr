"""Resolution of the deployed environment a smoke run tests against.

Production and staging commit their ``client.toml`` to the repo, so naming the
tier is enough to reach it. Every other env keeps its own copy under its state
root, which ``minds-admin env activate`` points at through
``MINDS_CLIENT_CONFIG_PATH``; the suite requires that export rather than
guessing at a data-root layout.
"""

import os
from pathlib import Path
from typing import Final

from pydantic import AnyUrl

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests.data_types import SmokeBackend
from imbue.minds.smoke_tests.primitives import ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SMOKE_TIER_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError
from imbue.mngr_imbue_cloud.primitives import PRODUCTION_TIER
from imbue.mngr_imbue_cloud.primitives import STAGING_TIER
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name

# The tiers whose client.toml and deploy.toml are committed to this repo.
COMMITTED_CONFIG_TIERS: Final[frozenset[str]] = frozenset({PRODUCTION_TIER, STAGING_TIER})


def resolve_smoke_client_config_path(env_name: DevEnvName) -> Path:
    """The ``client.toml`` a smoke run reads ``env_name``'s URLs from."""
    tier = tier_for_env_name(str(env_name))
    if tier in COMMITTED_CONFIG_TIERS:
        committed_path = repo_tier_client_config_path(tier)
        if not committed_path.is_file():
            raise SmokeConfigError(f"The committed {tier} client config is missing at {committed_path}.")
        return committed_path
    activated_path = os.environ.get(ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR)
    if not activated_path:
        raise SmokeConfigError(
            f"{SMOKE_TIER_ENV_VAR}={str(env_name)!r} is a {tier} env, whose client.toml is not committed, and "
            f"{ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR} is unset. Activate the env first "
            f'(`eval "$(uv run minds-admin env activate {env_name})"`), which the smoke just recipes do.'
        )
    path = Path(activated_path)
    if not path.is_file():
        raise SmokeConfigError(
            f"{ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR}={activated_path!r} does not exist; deploy env "
            f"{str(env_name)!r} before smoke-testing it."
        )
    return path


def resolve_smoke_backend(env_name: DevEnvName, client_config_path: Path) -> SmokeBackend:
    """Combine the env's client config with its tier's deploy config into the surfaces a run probes."""
    tier = tier_for_env_name(str(env_name))
    client_config = load_client_config(client_config_path)
    deploy_config = load_deploy_config(tier)
    origins = deploy_config.origins
    return SmokeBackend(
        env_name=env_name,
        tier=NonEmptyStr(tier),
        connector_url=client_config.connector_url,
        litellm_proxy_url=client_config.litellm_proxy_url,
        accounts_origin=origins.accounts_origin
        if origins is not None
        else AnyUrl(client_config.accounts_origin_url()),
        chrome_origin=origins.chrome_origin if origins is not None else None,
        legacy_chrome_origins=origins.legacy_chrome_origins if origins is not None else (),
        update_feed_base_url=client_config.update_feed_base_url,
        is_waitlist_enabled=deploy_config.waitlist.is_enabled if deploy_config.waitlist is not None else False,
    )
