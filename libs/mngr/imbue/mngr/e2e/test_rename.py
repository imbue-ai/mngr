"""Tests for renaming agents.

The tutorial only contains an informational comment about ``mngr rename`` and
no executable block, so these tests live outside the tutorial/ subdirectory.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100104",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("my-task -> renamed-task")

    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify only the new name appears and agent is still alive",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    agent_names = [a["name"] for a in agents]
    assert agent_names == ["renamed-task"], f"Expected only 'renamed-task', got {agent_names}"
    # A rename relabels the existing agent in place rather than destroying and
    # recreating it: the single surviving agent must keep its original command
    # and still be alive (not STOPPED/FAILED).
    (renamed_agent,) = agents
    assert renamed_agent["command"] == "sleep 100104", renamed_agent
    assert renamed_agent["state"] in ("WAITING", "RUNNING"), renamed_agent

    exec_result = e2e.run(
        "mngr exec renamed-task 'ps aux | grep sleep'",
        comment="Verify the renamed agent is still running its command",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 100104")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_rename_dry_run_does_not_rename(e2e: E2eSession) -> None:
    """``mngr rename --dry-run`` previews the rename without applying it."""
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100104",
            comment="Create agent for dry-run rename",
        )
    ).to_succeed()

    dry_run_result = e2e.run(
        "mngr rename my-task renamed-task --dry-run",
        comment="Preview the rename without applying it",
    )
    expect(dry_run_result).to_succeed()
    expect(dry_run_result.stdout).to_contain("Would rename agent: my-task -> renamed-task")

    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify the agent still has its original name (dry-run did not mutate)",
    )
    expect(list_result).to_succeed()
    agent_names = [a["name"] for a in json.loads(list_result.stdout)["agents"]]
    assert agent_names == ["my-task"], f"Expected dry-run to leave 'my-task' unchanged, got {agent_names}"

    exec_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the original agent is still reachable and running its command",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 100104")
