from pathlib import Path

import pytest

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.smoke_tests.backend import resolve_smoke_backend
from imbue.minds.smoke_tests.backend import resolve_smoke_client_config_path
from imbue.minds.smoke_tests.primitives import ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR
from imbue.minds.smoke_tests.primitives import SmokeConfigError

_COMMITTED_TIERS = ("production", "staging")


@pytest.mark.parametrize("tier", _COMMITTED_TIERS)
def test_a_committed_tier_is_resolved_from_the_repo(tier: str) -> None:
    config_path = resolve_smoke_client_config_path(DevEnvName(tier))
    assert config_path.is_file()
    assert config_path.parent.name == tier


@pytest.mark.parametrize("tier", _COMMITTED_TIERS)
def test_a_committed_tier_s_backend_carries_every_surface_a_run_probes(tier: str) -> None:
    backend = resolve_smoke_backend(DevEnvName(tier), resolve_smoke_client_config_path(DevEnvName(tier)))

    assert str(backend.env_name) == tier
    assert str(backend.tier) == tier
    assert str(backend.connector_url).startswith("https://")
    assert str(backend.litellm_proxy_url).startswith("https://")
    assert str(backend.accounts_origin).startswith("https://")


def test_a_dev_env_without_an_activation_says_how_to_activate_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR, raising=False)
    with pytest.raises(SmokeConfigError, match="env activate"):
        resolve_smoke_client_config_path(DevEnvName("dev-smoke"))


def test_a_dev_env_whose_activated_config_is_absent_says_to_deploy_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR, str(tmp_path / "client.toml"))
    with pytest.raises(SmokeConfigError, match="before smoke-testing it"):
        resolve_smoke_client_config_path(DevEnvName("dev-smoke"))


def test_a_dev_env_is_resolved_from_its_activated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    activated_config = tmp_path / "client.toml"
    activated_config.write_text(
        'connector_url = "https://rsc-dev-smoke.example.run"\nlitellm_proxy_url = "https://llm-dev-smoke.example.run"\n'
    )
    monkeypatch.setenv(ACTIVATED_CLIENT_CONFIG_PATH_ENV_VAR, str(activated_config))

    env_name = DevEnvName("dev-smoke")
    backend = resolve_smoke_backend(env_name, resolve_smoke_client_config_path(env_name))

    assert str(backend.tier) == "dev"
    assert str(backend.connector_url).startswith("https://rsc-dev-smoke.example.run")
    # A dev tier has no dedicated accounts domain, so the connector serves its pages.
    assert str(backend.accounts_origin).startswith("https://rsc-dev-smoke.example.run")
