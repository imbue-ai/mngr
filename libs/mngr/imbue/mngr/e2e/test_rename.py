"""Tests for renaming agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # "rename" is an experimental command. See "mngr rename --help" for current usage.
    """)

    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100104",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    # Capture the agent ID before rename so we can verify identity is preserved
    # (rename should update only the name, not destroy and recreate the agent).
    before_result = e2e.run(
        "mngr list --format json",
        comment="Capture original agent ID before rename",
    )
    expect(before_result).to_succeed()
    before_agents = json.loads(before_result.stdout)["agents"]
    assert [a["name"] for a in before_agents] == ["my-task"]
    original_agent_id = before_agents[0]["id"]

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("my-task -> renamed-task")

    # Verify via JSON list that only the new name exists, the agent ID is
    # preserved, and the agent is still alive.
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify only the new name appears and agent identity is preserved",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    agent_names = [a["name"] for a in agents]
    assert agent_names == ["renamed-task"], f"Expected only 'renamed-task', got {agent_names}"
    assert agents[0]["id"] == original_agent_id, (
        f"Agent ID changed during rename: {original_agent_id} -> {agents[0]['id']}"
    )

    # Verify the renamed agent is still functional
    exec_result = e2e.run(
        "mngr exec renamed-task 'ps aux | grep sleep'",
        comment="Verify the renamed agent is still running its command",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 100104")

    # Verify the old name no longer resolves (proving the rename was complete)
    old_name_result = e2e.run(
        "mngr exec my-task pwd",
        comment="Verify the old name no longer resolves to an agent",
    )
    expect(old_name_result).to_fail()
