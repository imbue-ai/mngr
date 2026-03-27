"""Conftest for minds package-level tests (e.g. end-to-end release tests).

The created_test_coder fixture is module-scoped so that multiple tests
sharing it reuse a single agent, avoiding redundant creation cycles.
"""

import shutil
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from loguru import logger

from imbue.minds.testing import find_agent
from imbue.minds.testing import init_and_commit_git_repo
from imbue.minds.testing import run_mngr
from imbue.mngr.utils.polling import wait_for


@pytest.fixture(scope="module")
def created_test_coder() -> Generator[dict[str, object], None, None]:
    """Create a test-coder mind agent and yield its agent record.

    Module-scoped so all tests in the module share a single agent,
    avoiding redundant creation cycles (~30s each). Handles creation and
    cleanup so individual tests only need to exercise the created agent.

    Waits for provisioning to complete (mngr create backgrounds provisioning
    when called with --no-connect) before yielding.
    """
    agent_name = "e2e-test-{}".format(uuid4().hex)
    agent_id = "agent-{}".format(uuid4().hex)

    # Create a minimal mind directory with a git repo
    mind_dir = Path.home() / ".minds" / agent_id
    mind_dir.mkdir(parents=True, exist_ok=True)

    init_result = run_mngr("--version")
    assert init_result.returncode == 0, "mngr not available: {}".format(init_result.stderr)

    # Initialize git repo in mind dir
    init_and_commit_git_repo(mind_dir, mind_dir.parent, allow_empty=True)

    # Create the agent directly via mngr create (cwd must be mind_dir for --transfer=none)
    create_result = run_mngr(
        "create",
        agent_name,
        "--id",
        agent_id,
        "--no-connect",
        "--type",
        "test-coder",
        "--label",
        "mind=true",
        "--yes",
        "--transfer=none",
        cwd=mind_dir,
    )
    assert create_result.returncode == 0, "mngr create failed:\nstdout: {}\nstderr: {}".format(
        create_result.stdout, create_result.stderr
    )

    agent = find_agent(agent_name)
    assert agent is not None, "Agent {} not found in mngr list".format(agent_name)

    settings_path = Path(str(agent["work_dir"])) / "minds.toml"
    wait_for(
        condition=settings_path.exists,
        timeout=60.0,
        poll_interval=1.0,
        error_message="Provisioning did not complete within 60s (waiting for {})".format(settings_path),
    )

    try:
        yield agent
    finally:
        _cleanup_agent(agent_name, agent_id)


def _cleanup_agent(agent_name: str, agent_id: str) -> None:
    """Destroy an agent and clean up its mind directory."""
    result = run_mngr("destroy", agent_name, "--force", timeout=30.0)
    if result.returncode != 0:
        logger.warning("Failed to destroy agent {}: {}", agent_name, result.stderr[:200])

    mind_dir = Path.home() / ".minds" / agent_id
    if mind_dir.exists():
        shutil.rmtree(mind_dir, ignore_errors=True)
