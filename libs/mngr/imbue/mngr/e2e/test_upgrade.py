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
@pytest.mark.timeout(60)
def test_preexisting_agent_state_discovered(minimal_install_env: MinimalInstallEnv) -> None:
    """mngr list should discover agents from pre-existing state on disk.

    This simulates an upgrade scenario: agent state files written by an older
    version of mngr should still be found by the current version's discovery.
    """
    host_dir = Path(minimal_install_env.env["MNGR_HOST_DIR"])
    host_id = uuid.uuid4().hex
    agent_id = uuid.uuid4().hex

    # Write host state
    host_dir_path = host_dir / "hosts" / host_id
    host_dir_path.mkdir(parents=True)
    (host_dir_path / "state.json").write_text(
        json.dumps(
            {
                "id": host_id,
                "name": "local",
                "provider": "local",
            }
        )
    )

    # Write agent state under the host
    agent_dir = host_dir_path / "agents" / agent_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "state.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "name": "pre-existing-agent",
                "type": "claude",
                "host_id": host_id,
                "work_dir": str(minimal_install_env.repo_dir),
            }
        )
    )

    # mngr list should find the pre-existing agent
    result = minimal_install_env.run_mngr(["list"])
    assert result.returncode == 0, (
        f"mngr list failed (exit {result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "pre-existing-agent" in result.stdout, (
        f"Expected 'pre-existing-agent' in mngr list output:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
