"""Tests for version upgrade and backward compatibility scenarios."""

import subprocess

import pytest

from imbue.mngr.conftest import MinimalInstallEnv


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_with_unknown_keys_strict(minimal_install_env: MinimalInstallEnv) -> None:
    """In strict mode (the default), unknown config keys should produce a clear error."""
    config_dir = minimal_install_env.repo_dir / f".{minimal_install_env.env['MNGR_ROOT_NAME']}"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.toml"
    config_file.write_text("future_feature = true\nheadless = true\n")

    result = minimal_install_env.run_mngr(["list"])
    assert result.returncode != 0, (
        f"Expected mngr to fail with unknown config keys in strict mode, but it succeeded:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "future_feature" in combined.lower() or "unknown" in combined.lower(), (
        f"Error should mention the unknown field:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_config_with_unknown_keys_non_strict(minimal_install_env: MinimalInstallEnv) -> None:
    """With MNGR_ALLOW_UNKNOWN_CONFIG, unknown keys should be warned but not fatal."""
    config_dir = minimal_install_env.repo_dir / f".{minimal_install_env.env['MNGR_ROOT_NAME']}"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "settings.toml"
    config_file.write_text("future_feature = true\nheadless = true\n")

    env = {**minimal_install_env.env, "MNGR_ALLOW_UNKNOWN_CONFIG": "1"}
    mngr_bin = str(minimal_install_env.venv_dir / "bin" / "mngr")
    # Use a config-loading command that does not depend on any provider backend
    # (e.g. Docker) so the assertion isolates the config-parsing behavior the
    # docstring scopes: the unknown key is warned about but does not abort the
    # load. `config get headless` reads a known scalar and exits 0 once config
    # loads successfully.
    result = subprocess.run(
        [mngr_bin, "config", "get", "headless"],
        capture_output=True,
        text=True,
        cwd=minimal_install_env.repo_dir,
        env=env,
        timeout=30,
    )
    # "not fatal": config loading succeeded, so the command exits 0.
    assert result.returncode == 0, (
        f"config loading should succeed with MNGR_ALLOW_UNKNOWN_CONFIG=1:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # "warned": the unknown key is surfaced as a warning rather than swallowed.
    assert "future_feature" in result.stderr, (
        f"Expected a warning naming the unknown field 'future_feature':\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
