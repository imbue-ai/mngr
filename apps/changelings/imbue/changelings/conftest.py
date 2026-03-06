"""Conftest for changelings package-level tests (e.g. end-to-end release tests).

The deployed_test_coder fixture is module-scoped so that multiple tests
sharing it reuse a single deployed agent, avoiding redundant deploy cycles.
"""

import shutil
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger

from imbue.changelings.testing import find_agent
from imbue.changelings.testing import run_changeling
from imbue.changelings.testing import run_mng
from imbue.mng.utils.polling import wait_for


@pytest.fixture(scope="module")
def deployed_test_coder() -> Generator[dict[str, object], None, None]:
    """Deploy a test-coder changeling and yield its agent record.

    Module-scoped so all tests in the module share a single deployed agent,
    avoiding redundant deploy cycles (~30s each). Handles deployment and
    cleanup so individual tests only need to exercise the deployed agent.

    Waits for provisioning to complete (mng create backgrounds provisioning
    when called with --no-connect) before yielding.
    """
    agent_name = f"e2e-test-{uuid4().hex}"

    deploy_result = run_changeling(
        "deploy",
        "--agent-type",
        "test-coder",
        "--name",
        agent_name,
        "--provider",
        "local",
        "--no-self-deploy",
    )
    assert deploy_result.returncode == 0, (
        f"Deploy failed:\nstdout: {deploy_result.stdout}\nstderr: {deploy_result.stderr}"
    )

    agent = find_agent(agent_name)
    assert agent is not None, f"Agent {agent_name} not found in mng list"

    agent_id = str(agent["id"])
    settings_path = Path(str(agent["work_dir"])) / ".changelings" / "settings.toml"
    wait_for(
        condition=settings_path.exists,
        timeout=60.0,
        poll_interval=1.0,
        error_message=f"Provisioning did not complete within 60s (waiting for {settings_path})",
    )

    try:
        yield agent
    finally:
        _cleanup_agent(agent_name, agent_id)


def _cleanup_agent(agent_name: str, agent_id: str) -> None:
    """Destroy an agent and clean up its changeling directory."""
    result = run_mng("destroy", agent_name, "--force", timeout=30.0)
    if result.returncode != 0:
        logger.warning("Failed to destroy agent {}: {}", agent_name, result.stderr[:200])

    changeling_dir = Path.home() / ".changelings" / agent_id
    if changeling_dir.exists():
        shutil.rmtree(changeling_dir, ignore_errors=True)
