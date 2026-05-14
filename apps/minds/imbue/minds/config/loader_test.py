from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.config.data_types import DeployEnvConfig
from imbue.minds.config.loader import EnvConfigError
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.config.loader import resolve_default_client_config_path

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
    assert config.vault_path_prefix == "secrets/kv/minds/dev"
    assert "cloudflare" in config.secrets.services
    assert "supertokens" in config.secrets.services


def test_load_deploy_config_unknown_tier_raises() -> None:
    with pytest.raises(EnvConfigError, match="No deploy config found for tier"):
        load_deploy_config("not_a_real_tier")


def test_resolve_default_client_config_path_falls_back_to_dev() -> None:
    """With no _bundled/client.toml in the repo, the resolver returns dev/client.toml.

    The dev fallback is a stable, committed file; the bundled path is
    gitignored. This test is robust under fresh checkouts.
    """
    path = resolve_default_client_config_path()
    assert path.name == "client.toml"
    assert path.parent.name == "dev" or path.parent.name == "_bundled"
