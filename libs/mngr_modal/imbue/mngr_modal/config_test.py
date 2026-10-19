from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.loader import _parse_mngr_env_overrides
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.provider_config_registry import register_provider_config
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.testing import setup_layered_config_test_env
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.mngr_modal.config import read_modal_profiles


def test_tokens_default_to_unset() -> None:
    config = ModalProviderConfig()
    assert config.token_id is None
    assert config.token_secret is None


def test_tokens_set_together_are_accepted_and_the_secret_is_masked() -> None:
    config = ModalProviderConfig(token_id="ak-test", token_secret=SecretStr("as-test"))
    assert config.token_id == "ak-test"
    assert config.token_secret is not None
    assert config.token_secret.get_secret_value() == "as-test"
    assert "as-test" not in repr(config)


def test_a_token_split_across_config_layers_loads_as_the_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, temp_git_repo_cwd: Path, cg: ConcurrencyGroup
) -> None:
    """Each layer is parsed alone, so the id in settings.toml plus the secret from the environment must load."""
    pm, project_dir = setup_layered_config_test_env(monkeypatch, tmp_path)
    register_provider_config("modal", ModalProviderConfig)
    (project_dir / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[providers.work_modal]\nbackend = "modal"\ntoken_id = "ak-file"\n'
    )
    # A layer for a named instance restates its backend, else the loader resolves the backend from the name.
    monkeypatch.setenv("MNGR__PROVIDERS__WORK_MODAL__BACKEND", "modal")
    monkeypatch.setenv("MNGR__PROVIDERS__WORK_MODAL__TOKEN_SECRET", "as-env")

    mngr_ctx = load_config(pm=pm, concurrency_group=cg)

    config = mngr_ctx.config.providers[ProviderInstanceName("work_modal")]
    assert isinstance(config, ModalProviderConfig)
    assert config.token_id == "ak-file"
    assert config.token_secret is not None
    assert config.token_secret.get_secret_value() == "as-env"


def test_tokens_arrive_through_the_mngr_env_override_path() -> None:
    """``MNGR__PROVIDERS__<NAME>__TOKEN_ID`` / ``..._TOKEN_SECRET`` land on the provider block."""
    raw = _parse_mngr_env_overrides(
        {"MNGR__PROVIDERS__MODAL__TOKEN_ID": "ak-env", "MNGR__PROVIDERS__MODAL__TOKEN_SECRET": "as-env"}
    )
    config = ModalProviderConfig.model_validate(raw["providers"]["modal"])
    assert config.token_id == "ak-env"
    assert config.token_secret is not None
    assert config.token_secret.get_secret_value() == "as-env"


def test_read_modal_profiles_lists_the_sections_holding_a_token_pair_in_file_order(tmp_path: Path) -> None:
    config_path = tmp_path / "modal.toml"
    config_path.write_text(
        '[work]\ntoken_id = "ak-w"\ntoken_secret = "as-w"\n\n'
        '[default]\ntoken_id = "ak-d"\ntoken_secret = "as-d"\nactive = true\n\n'
        '[half]\ntoken_id = "ak-h"\n\n[blank]\ntoken_id = ""\ntoken_secret = "as-b"\n\nloose = "not a section"\n'
    )

    profiles = read_modal_profiles(config_path)

    assert list(profiles) == ["work", "default"]
    assert profiles["work"].token_id == "ak-w"
    assert profiles["work"].token_secret.get_secret_value() == "as-w"
    with pytest.raises(FileNotFoundError):
        read_modal_profiles(tmp_path / "missing.toml")
