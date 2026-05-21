"""Tests for ``minds env activate``'s use-vs-deploy split.

Covers:

- Plain ``activate <name>``: exports use-side vars, emits ``unset
  MODAL_PROFILE``, never emits ``export MODAL_PROFILE=...``.
- ``activate --deploy <name>``: also exports ``MODAL_PROFILE`` pinned to
  the tier's ``modal_workspace``, after validating ``~/.modal.toml`` has
  a matching profile.
- ``activate --deploy <name>`` fails up front when ``~/.modal.toml``
  lacks the required profile.
- ``env deploy`` / ``env destroy`` refuse to run when the shell is not
  deploy-activated, and the refusal message points at the right
  ``--deploy`` re-activation command.
- ``env deactivate`` continues to ``unset`` MODAL_PROFILE so a previously
  deploy-activated shell fully clears.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.minds.cli._activated_env import MODAL_PROFILE_ENV_VAR
from imbue.minds.cli.env import env


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip activation env vars; tests opt in to a specific env explicitly."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    monkeypatch.delenv(MODAL_PROFILE_ENV_VAR, raising=False)
    return tmp_path


def _write_modal_toml_with_profile(home: Path, workspace: str) -> None:
    """Drop a minimal ``~/.modal.toml`` with one section so deploy-mode validation passes."""
    (home / ".modal.toml").write_text(f'[{workspace}]\ntoken_id = "ak-1"\ntoken_secret = "as-1"\n')


# -- activate: use-only mode --


def test_activate_dev_env_use_only_omits_modal_profile_export(_isolated_env: Path) -> None:
    """Plain ``activate --create`` emits no ``export MODAL_PROFILE=`` line."""
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "export MINDS_ROOT_NAME=minds-dev-foo" in result.output
    assert "export MNGR_HOST_DIR=" in result.output
    assert "export MNGR_PREFIX=" in result.output
    assert "export MINDS_CLIENT_CONFIG_PATH=" in result.output
    # The key contract: no MODAL_PROFILE export in use-only mode.
    assert "export MODAL_PROFILE=" not in result.output


def test_activate_dev_env_use_only_emits_unset_modal_profile(_isolated_env: Path) -> None:
    """Plain ``activate`` emits ``unset MODAL_PROFILE`` so a deploy-mode shell flips back cleanly."""
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "unset MODAL_PROFILE" in result.output


def test_activate_dev_env_use_only_does_not_validate_modal_toml(_isolated_env: Path) -> None:
    """A missing ``~/.modal.toml`` is fine for use-only activation."""
    runner = CliRunner()
    assert not (_isolated_env / ".modal.toml").exists()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output


# -- activate: deploy mode --


def test_activate_dev_env_deploy_mode_exports_modal_profile(_isolated_env: Path) -> None:
    """``--deploy`` exports ``MODAL_PROFILE`` pinned to the dev tier's modal_workspace."""
    _write_modal_toml_with_profile(_isolated_env, "minds-dev")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "--deploy", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "export MODAL_PROFILE=minds-dev" in result.output
    # And no contradictory unset.
    assert "unset MODAL_PROFILE" not in result.output


def test_activate_deploy_mode_refuses_when_modal_toml_lacks_profile(_isolated_env: Path) -> None:
    """No matching ``~/.modal.toml`` profile = clean refusal with a ``modal token set`` hint."""
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "--deploy", "dev-foo"])
    assert result.exit_code != 0, result.output
    assert "modal token set --profile minds-dev" in result.output


def test_activate_deploy_mode_refuses_when_modal_toml_has_wrong_profile(_isolated_env: Path) -> None:
    """A ``~/.modal.toml`` with a different profile still trips the refusal."""
    _write_modal_toml_with_profile(_isolated_env, "some-other-workspace")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "--deploy", "dev-foo"])
    assert result.exit_code != 0, result.output
    assert "no profile named 'minds-dev'" in result.output


# -- env deploy / destroy gate --


def test_env_deploy_refuses_without_deploy_activation(_isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``minds env deploy`` requires deploy-mode activation; refuses otherwise."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-foo")
    runner = CliRunner()
    result = runner.invoke(env, ["deploy"], obj={})
    assert result.exit_code != 0, result.output
    assert "use only" in result.output
    assert "minds env activate --deploy dev-foo" in result.output


def test_env_deploy_refuses_with_mismatched_modal_profile(
    _isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``MODAL_PROFILE`` that does not match the tier is a hard error."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-foo")
    monkeypatch.setenv(MODAL_PROFILE_ENV_VAR, "some-other-workspace")
    runner = CliRunner()
    result = runner.invoke(env, ["deploy"], obj={})
    assert result.exit_code != 0, result.output
    assert "some-other-workspace" in result.output
    assert "minds-dev" in result.output


def test_env_destroy_refuses_without_deploy_activation(_isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``minds env destroy`` shares the same deploy-mode gate as deploy."""
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-foo")
    runner = CliRunner()
    result = runner.invoke(env, ["destroy"], obj={})
    assert result.exit_code != 0, result.output
    assert "minds env activate --deploy dev-foo" in result.output


# -- deactivate --


def test_deactivate_unsets_modal_profile(_isolated_env: Path) -> None:
    """A deactivated shell drops every var either activation mode might have exported."""
    runner = CliRunner()
    result = runner.invoke(env, ["deactivate"])
    assert result.exit_code == 0, result.output
    assert "unset MODAL_PROFILE" in result.output
    assert "unset MINDS_ROOT_NAME" in result.output
    assert "unset MNGR_HOST_DIR" in result.output
    assert "unset MNGR_PREFIX" in result.output
    assert "unset MINDS_CLIENT_CONFIG_PATH" in result.output
