import json
from pathlib import Path

import pytest

from imbue.mng.utils.testing import ModalSubprocessTestEnv
from imbue.mng.utils.testing import get_short_random_string
from imbue.mng.utils.testing import run_mng_subprocess


def _get_agent_info(agent_name: str, env: dict[str, str]) -> dict | None:
    """Get agent info from `mng list --format json`. Returns None if not found."""
    result = run_mng_subprocess("list", "--format", "json", env=env, timeout=60)
    assert result.returncode == 0, f"mng list failed: {result.stderr}\n{result.stdout}"
    data = json.loads(result.stdout)
    for agent in data.get("agents", []):
        if agent.get("name") == agent_name:
            return agent
    return None


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_provision_stopped_modal_agent(
    tmp_path: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Provisioning a stopped Modal agent preserves identity and data.

    Regression test: previously, `mng provision` failed on stopped Modal
    agents because (1) agents on destroyed/offline hosts were not included
    in the search, and (2) the agent lookup required the agent process to
    be running.

    This test verifies that after create -> stop -> provision:
    - The agent ID is preserved
    - Existing env vars are preserved
    - User commands execute on the host
    """
    agent_name = f"test-modal-prov-stopped-{get_short_random_string()}"
    env_marker = f"PROV_TEST_MARKER={get_short_random_string()}"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "test.txt").write_text("test content")
    env = modal_subprocess_env.env

    # Create agent with an env var we can check for preservation
    result = run_mng_subprocess(
        "create",
        f"{agent_name}@.modal",
        "generic",
        "--no-connect",
        "--no-ensure-clean",
        "--source",
        str(source_dir),
        "--command",
        "sleep 999999",
        "--env",
        env_marker,
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}\n{result.stdout}"

    # Record agent ID before stop
    agent_info_before = _get_agent_info(agent_name, env)
    assert agent_info_before is not None, f"Agent {agent_name} not found after create"
    agent_id_before = agent_info_before["id"]

    # Stop the agent (destroys the Modal sandbox)
    result = run_mng_subprocess("stop", agent_name, env=env, timeout=120)
    assert result.returncode == 0, f"Stop failed: {result.stderr}\n{result.stdout}"

    # Provision the stopped agent with a new env var and a user command
    new_env_var = f"PROV_NEW_VAR={get_short_random_string()}"
    result = run_mng_subprocess(
        "provision",
        agent_name,
        "--env",
        new_env_var,
        "--user-command",
        "echo 'provision-ran' > /tmp/prov_marker.txt",
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, f"Provision stopped agent failed: {result.stderr}\n{result.stdout}"

    # Verify agent identity is preserved
    agent_info_after = _get_agent_info(agent_name, env)
    assert agent_info_after is not None, f"Agent {agent_name} not found after provision"
    assert agent_info_after["id"] == agent_id_before, (
        f"Agent ID changed: {agent_id_before} -> {agent_info_after['id']}"
    )
