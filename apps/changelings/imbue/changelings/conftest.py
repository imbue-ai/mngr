"""Conftest for changelings package-level tests (e.g. end-to-end release tests).

The deployed_test_coder fixture is module-scoped so that multiple tests
sharing it reuse a single deployed agent, avoiding redundant deploy cycles.
"""

import shutil
import time
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.changelings.testing import find_agent
from imbue.changelings.testing import run_changeling
from imbue.changelings.testing import run_mng


def _wait_for_provisioning(work_dir: str, max_wait_seconds: float = 60.0) -> None:
    """Wait for changeling provisioning to complete.

    mng create runs provisioning in a background process (forked child)
    when called with --no-connect. We poll for the .changelings/settings.toml
    file that TestCoderAgent.provision() writes as its last step.
    """
    settings_path = Path(work_dir) / ".changelings" / "settings.toml"
    deadline = time.monotonic() + max_wait_seconds
    while time.monotonic() < deadline:
        if settings_path.exists():
            return
        time.sleep(1.0)
    raise AssertionError(f"Provisioning did not complete within {max_wait_seconds}s (waiting for {settings_path})")


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

    _wait_for_provisioning(str(agent["work_dir"]))

    try:
        yield agent
    finally:
        _cleanup_agent(agent_name)


def _cleanup_agent(agent_name: str) -> None:
    """Destroy an agent and clean up its changeling directory."""
    agent = find_agent(agent_name)
    agent_id = str(agent["id"]) if agent else None

    run_mng("destroy", agent_name, "--force", timeout=30.0)

    if agent_id:
        changeling_dir = Path.home() / ".changelings" / agent_id
        if changeling_dir.exists():
            shutil.rmtree(changeling_dir, ignore_errors=True)
