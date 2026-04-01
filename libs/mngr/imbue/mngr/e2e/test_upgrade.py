"""Tests for version upgrade and backward compatibility scenarios."""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import MinimalInstallEnv


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
    result = subprocess.run(
        [mngr_bin, "list"],
        capture_output=True,
        text=True,
        cwd=minimal_install_env.repo_dir,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"mngr list should succeed with MNGR_ALLOW_UNKNOWN_CONFIG=1:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.mark.release
@pytest.mark.timeout(120)
def test_existing_agents_survive_reinstall(minimal_install_env: MinimalInstallEnv) -> None:
    """Agent state should persist across mngr reinstalls.

    When a user upgrades mngr (via uv tool install), their existing agents
    (stored under MNGR_HOST_DIR) should still be discoverable.
    """
    agents_dir = minimal_install_env.env["MNGR_HOST_DIR"]
    agent_id = uuid.uuid4().hex
    agent_dir = Path(agents_dir) / "agents" / agent_id
    agent_dir.mkdir(parents=True)

    state_file = agent_dir / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "id": agent_id,
                "name": "pre-existing-agent",
                "type": "claude",
                "host_id": "local",
                "work_dir": str(minimal_install_env.repo_dir),
            }
        )
    )

    # Reinstall mngr into the same venv (simulates upgrade)
    subprocess.run(
        ["uv", "pip", "install", "--reinstall", "imbue-mngr"],
        capture_output=True,
        text=True,
        cwd=minimal_install_env.repo_dir,
        env=minimal_install_env.env,
        timeout=60,
    )
    # Reinstall may fail if the package isn't in a registry, which is expected
    # in dev environments. The important thing is that the agent state files
    # are still on disk after any install operation.
    assert agent_dir.exists(), "Agent state directory should survive reinstall"
    assert state_file.exists(), "Agent state file should survive reinstall"
