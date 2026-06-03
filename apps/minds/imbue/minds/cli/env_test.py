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
from imbue.minds.cli.env import _destroy_agents_and_state_container_for_wipe
from imbue.minds.cli.env import env
from imbue.minds.envs.primitives import InvalidDevEnvNameError


def test_wipe_teardown_is_noop_without_profile_or_agents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # With no mngr profile + no agents under the env root, there is nothing to
    # destroy and the state-container cleanup skips on an unresolved user_id --
    # a pure no-op that does not raise (no Docker daemon is even contacted).
    monkeypatch.setenv("HOME", str(tmp_path))
    _destroy_agents_and_state_container_for_wipe("staging")


def test_wipe_teardown_raises_on_invalid_env_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Errors are surfaced, not swallowed: a bad env name must raise so the
    # operator sees the problem rather than silently leaking resources.
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(InvalidDevEnvNameError):
        _destroy_agents_and_state_container_for_wipe("not a valid env name!!")


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Strip activation env vars; tests opt in to a specific env explicitly."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)
    monkeypatch.delenv(MODAL_PROFILE_ENV_VAR, raising=False)
    # Make sure no inherited MODAL_CONFIG_PATH redirects deploy-mode
    # validation away from the test's ~/.modal.toml fixture file.
    monkeypatch.delenv("MODAL_CONFIG_PATH", raising=False)
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


def test_activate_use_only_header_omits_deploy_flag(_isolated_env: Path) -> None:
    """The 'Source via:' header should omit --deploy when the user did not pass it."""
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert 'eval "$(uv run minds env activate dev-foo)"' in result.output
    assert "--deploy" not in result.output


def test_activate_deploy_mode_header_includes_deploy_flag(_isolated_env: Path) -> None:
    """The 'Source via:' header should include --deploy so re-sourcing preserves the mode."""
    _write_modal_toml_with_profile(_isolated_env, "minds-dev")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "--deploy", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert 'eval "$(uv run minds env activate --deploy dev-foo)"' in result.output


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


# -- activate: recover-target guard (catch-22 avoidance) --


def test_activate_allows_when_only_this_envs_recover_target_exists(
    _isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending recover-target for the env being activated must NOT block activation.

    Otherwise `minds env recover` (which requires an activated env) could
    never run to clear it -- the activate/recover catch-22.
    """
    monkeypatch.chdir(_isolated_env)
    # monorepo-root marker for find_monorepo_root
    (_isolated_env / "apps").mkdir()
    (_isolated_env / ".minds-deploy-recover-target-dev-foo.json").write_text("{}")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "export MINDS_ROOT_NAME=minds-dev-foo" in result.output


def test_activate_refuses_when_another_envs_recover_target_exists(
    _isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recover-target for a DIFFERENT env still blocks activating an unaffected env.

    This surfaces a forgotten failed deploy rather than letting the operator
    silently proceed past it.
    """
    monkeypatch.chdir(_isolated_env)
    (_isolated_env / "apps").mkdir()
    (_isolated_env / ".minds-deploy-recover-target-dev-other.json").write_text("{}")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code != 0, result.output
    assert "dev-other" in result.output


def test_activate_succeeds_when_run_outside_the_monorepo(_isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Activation works from outside the monorepo: the recover-target guard is
    skipped (no monorepo root means no recover-target file can exist there)
    rather than erroring on NotInMonorepoError.
    """
    # Deliberately do NOT create an `apps/` marker, so find_monorepo_root
    # raises NotInMonorepoError from this cwd and the guard tolerates it.
    monkeypatch.chdir(_isolated_env)
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "export MINDS_ROOT_NAME=minds-dev-foo" in result.output


def test_activate_allowed_for_affected_env_even_when_another_target_exists(
    _isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activating an affected env is allowed even when another env also has a
    pending recover-target; the other is surfaced as a warning, not a block."""
    monkeypatch.chdir(_isolated_env)
    (_isolated_env / "apps").mkdir()
    (_isolated_env / ".minds-deploy-recover-target-dev-foo.json").write_text("{}")
    (_isolated_env / ".minds-deploy-recover-target-dev-other.json").write_text("{}")
    runner = CliRunner()
    result = runner.invoke(env, ["activate", "--create", "dev-foo"])
    assert result.exit_code == 0, result.output
    assert "export MINDS_ROOT_NAME=minds-dev-foo" in result.output


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
