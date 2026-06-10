"""Unit tests for the env-name -> tier mapping and the deploy-mode gate
shared by minds CLI subcommands."""

from pathlib import Path

import click
import pytest

from imbue.minds.cli._activated_env import CI_TIER
from imbue.minds.cli._activated_env import DEV_TIER
from imbue.minds.cli._activated_env import MODAL_PROFILE_ENV_VAR
from imbue.minds.cli._activated_env import PRODUCTION_ENV_NAME
from imbue.minds.cli._activated_env import STAGING_ENV_NAME
from imbue.minds.cli._activated_env import require_deploy_mode_activation
from imbue.minds.cli._activated_env import tier_for_env_name
from imbue.minds.cli._activated_env import validate_modal_profile_exists_in_modal_toml


def test_tier_for_env_name_production() -> None:
    assert tier_for_env_name(PRODUCTION_ENV_NAME) == PRODUCTION_ENV_NAME


def test_tier_for_env_name_staging() -> None:
    assert tier_for_env_name(STAGING_ENV_NAME) == STAGING_ENV_NAME


def test_tier_for_env_name_dev_env_returns_dev() -> None:
    assert tier_for_env_name("dev-josh") == DEV_TIER
    assert tier_for_env_name("dev-alice-3") == DEV_TIER


def test_tier_for_env_name_ci_env_returns_ci() -> None:
    """Ephemeral CI envs minted by the deployment-tests orchestrator route to the ci tier."""
    assert tier_for_env_name("ci-20260518t140212z") == CI_TIER
    assert tier_for_env_name("ci-20260518t140212z-abcd") == CI_TIER


def test_tier_for_env_name_dev_prefixed_with_ci_substring_still_dev() -> None:
    """A dev-prefixed env whose user portion happens to contain 'ci' is still dev tier.

    Regression guard: the ci check is a prefix match on ``ci-``, not a
    substring search, so a name like ``dev-ci-leftover`` (an artifact of
    the old ``dev-ci-`` naming convention) must still route to dev.
    """
    assert tier_for_env_name("dev-ci-leftover") == DEV_TIER


# -- validate_modal_profile_exists_in_modal_toml --


def test_validate_modal_profile_accepts_matching_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    modal_toml = tmp_path / ".modal.toml"
    modal_toml.write_text('[minds-dev]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    # No exception.
    validate_modal_profile_exists_in_modal_toml("minds-dev")


def test_validate_modal_profile_rejects_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    with pytest.raises(click.ClickException) as excinfo:
        validate_modal_profile_exists_in_modal_toml("minds-staging")
    message = str(excinfo.value)
    assert "not found" in message
    assert str(tmp_path / ".modal.toml") in message
    assert "modal token set --profile minds-staging" in message


def test_validate_modal_profile_rejects_missing_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    modal_toml = tmp_path / ".modal.toml"
    modal_toml.write_text('[minds-dev]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    with pytest.raises(click.ClickException) as excinfo:
        validate_modal_profile_exists_in_modal_toml("minds-staging")
    message = str(excinfo.value)
    assert "no profile named 'minds-staging'" in message
    assert "modal token set --profile minds-staging" in message


def test_validate_modal_profile_rejects_unparseable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    modal_toml = tmp_path / ".modal.toml"
    modal_toml.write_text("this is not valid toml = = =")
    with pytest.raises(click.ClickException) as excinfo:
        validate_modal_profile_exists_in_modal_toml("minds-dev")
    message = str(excinfo.value)
    assert "Could not read" in message
    assert str(tmp_path / ".modal.toml") in message


def test_validate_modal_profile_rejects_non_table_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare scalar at the workspace key is not a valid profile."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
    modal_toml = tmp_path / ".modal.toml"
    # A top-level scalar key collides namespace-wise with the workspace
    # name but does not satisfy "section named workspace".
    modal_toml.write_text('"minds-dev" = "not a table"\n')
    with pytest.raises(click.ClickException):
        validate_modal_profile_exists_in_modal_toml("minds-dev")


def test_validate_modal_profile_honors_modal_config_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When MODAL_CONFIG_PATH is set, validation must read that file (matching the Modal SDK)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Profile lives in the override path, NOT in ~/.modal.toml.
    override_path = tmp_path / "alt-modal-config.toml"
    override_path.write_text('[minds-dev]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(override_path))
    # Validation reads the override path and finds the profile.
    validate_modal_profile_exists_in_modal_toml("minds-dev")


def test_validate_modal_profile_rejects_when_override_lacks_profile_even_if_home_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With MODAL_CONFIG_PATH set, ~/.modal.toml is ignored entirely (mirrors Modal SDK)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Profile present in HOME but not the override -- validation must
    # still fail, because Modal SDK would read the override and miss it.
    home_toml = tmp_path / ".modal.toml"
    home_toml.write_text('[minds-dev]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    override_path = tmp_path / "alt-modal-config.toml"
    override_path.write_text('[some-other-profile]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')
    monkeypatch.setenv("MODAL_CONFIG_PATH", str(override_path))
    with pytest.raises(click.ClickException) as excinfo:
        validate_modal_profile_exists_in_modal_toml("minds-dev")
    message = str(excinfo.value)
    assert str(override_path) in message
    assert "no profile named 'minds-dev'" in message


# -- require_deploy_mode_activation --


def test_require_deploy_mode_passes_when_modal_profile_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev tier's modal_workspace is `minds-dev` (per the committed deploy.toml)."""
    monkeypatch.setenv(MODAL_PROFILE_ENV_VAR, "minds-dev")
    # No exception.
    require_deploy_mode_activation(env_name="dev-foo", tier=DEV_TIER)


def test_require_deploy_mode_rejects_unset_modal_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODAL_PROFILE_ENV_VAR, raising=False)
    with pytest.raises(click.ClickException) as excinfo:
        require_deploy_mode_activation(env_name="dev-foo", tier=DEV_TIER)
    message = str(excinfo.value)
    assert "use only" in message
    assert "MODAL_PROFILE pinned to 'minds-dev'" in message
    assert "minds env activate --deploy dev-foo" in message


def test_require_deploy_mode_rejects_mismatched_modal_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODAL_PROFILE_ENV_VAR, "some-other-workspace")
    with pytest.raises(click.ClickException) as excinfo:
        require_deploy_mode_activation(env_name="dev-foo", tier=DEV_TIER)
    message = str(excinfo.value)
    assert "some-other-workspace" in message
    assert "minds-dev" in message
    assert "minds env activate --deploy dev-foo" in message


def test_require_deploy_mode_passes_for_staging_with_matching_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODAL_PROFILE_ENV_VAR, "minds-staging")
    # No exception -- staging deploy.toml ships with modal_workspace="minds-staging".
    require_deploy_mode_activation(env_name=STAGING_ENV_NAME, tier=STAGING_ENV_NAME)
