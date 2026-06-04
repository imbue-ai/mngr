from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import bundled_client_config_path_or_none
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.config.loader import repo_tier_client_config_path
from imbue.minds.envs.per_env_deploy import per_env_secret_services

_VALID_CLIENT_TOML = (
    'connector_url = "https://connector.example.com/"\nlitellm_proxy_url = "https://litellm.example.com/"\n'
)


def test_load_client_config_round_trip(tmp_path: Path) -> None:
    """A valid client TOML deserializes into the expected ClientEnvConfig."""
    path = tmp_path / "client.toml"
    path.write_text(_VALID_CLIENT_TOML)
    config = load_client_config(path)
    assert config == ClientEnvConfig(
        connector_url=AnyUrl("https://connector.example.com/"),
        litellm_proxy_url=AnyUrl("https://litellm.example.com/"),
    )


def test_load_client_config_missing_required_field(tmp_path: Path) -> None:
    """A TOML missing connector_url surfaces as EnvConfigError."""
    path = tmp_path / "client.toml"
    path.write_text('litellm_proxy_url = "https://litellm.example.com/"\n')
    with pytest.raises(EnvConfigError, match="Invalid client config"):
        load_client_config(path)


def test_load_client_config_invalid_url(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    path.write_text('connector_url = "not-a-url"\nlitellm_proxy_url = "https://litellm.example.com/"\n')
    with pytest.raises(EnvConfigError, match="Invalid client config"):
        load_client_config(path)


def test_load_client_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EnvConfigError, match="Cannot read client config"):
        load_client_config(tmp_path / "does_not_exist.toml")


def test_load_client_config_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    path.write_text("this is = not [valid toml")
    with pytest.raises(EnvConfigError, match="Failed to parse client config"):
        load_client_config(path)


def test_load_deploy_config_dev_tier_round_trip() -> None:
    """The committed dev/deploy.toml parses cleanly."""
    config = load_deploy_config("dev")
    assert isinstance(config, DeployEnvConfig)
    assert config.vault_path_prefix == "secrets/minds/dev"
    assert "cloudflare" in config.secrets.services
    assert "supertokens" in config.secrets.services


def test_load_deploy_config_ci_tier_round_trip() -> None:
    """The committed ci/deploy.toml parses cleanly."""
    config = load_deploy_config("ci")
    assert isinstance(config, DeployEnvConfig)
    assert config.vault_path_prefix == "secrets/minds/ci"
    assert "cloudflare" in config.secrets.services
    assert "supertokens" in config.secrets.services


@pytest.mark.parametrize("tier", ["dev", "staging", "production", "ci"])
def test_deploy_config_secrets_match_canonical_per_env_services(tier: str) -> None:
    """Every tier must push exactly the per-env secrets the deployed apps reference.

    Regression guard: the connector app references each
    ``<svc>-<tier>-<deploy_id>`` Modal Secret named in
    ``per_env_secret_services()`` via ``Secret.from_name``, so a tier whose
    ``[secrets].services`` omits one makes ``modal deploy`` fail with
    "Secret ... not found in environment". This caught a missing ``ovh`` entry
    across all tiers after the connector started signing OVH calls at runtime.
    """
    config = load_deploy_config(tier)
    assert set(config.secrets.services) == set(per_env_secret_services())


def test_load_deploy_config_unknown_tier_raises() -> None:
    with pytest.raises(EnvConfigError, match="No deploy config found for tier"):
        load_deploy_config("not_a_real_tier")


def test_load_client_config_rejects_extra_fields(tmp_path: Path) -> None:
    """The ClientEnvConfig model has extra='forbid' so a stray secrets table is rejected.

    This is one of the layers that keeps secrets out of a committed
    staging/production client.toml.
    """
    path = tmp_path / "client.toml"
    path.write_text(_VALID_CLIENT_TOML + '\n[secrets]\nFOO = "bar"\n')
    with pytest.raises(EnvConfigError, match="Invalid client config"):
        load_client_config(path)


def test_repo_tier_client_config_path_resolves_under_envs_dir() -> None:
    """The returned path is `apps/minds/imbue/minds/config/envs/<tier>/client.toml`."""
    path = repo_tier_client_config_path("staging")
    assert path.name == "client.toml"
    assert path.parent.name == "staging"
    assert path.parent.parent.name == "envs"


def test_bundled_client_config_path_or_none_default_is_none() -> None:
    """The committed repo has no `_bundled/client.toml` -- it ships empty.

    Build-time `bundleClientConfig()` writes the file when
    `MINDS_CLIENT_CONFIG_BUNDLE` is set; an uninstalled dev tree has
    nothing there.
    """
    assert bundled_client_config_path_or_none() is None
