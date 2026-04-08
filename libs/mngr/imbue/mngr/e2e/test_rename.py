"""Tests for renaming agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # "rename" is an experimental command. See "mngr rename --help" for current usage.
    """)

    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("my-task -> renamed-task")

    # Verify via JSON list that only the new name exists and agent is still alive
    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify only the new name appears and agent is still alive",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    agent_names = [a["name"] for a in agents]
    assert agent_names == ["renamed-task"], f"Expected only 'renamed-task', got {agent_names}"

    # Verify the renamed agent is still functional
    exec_result = e2e.run(
        "mngr exec renamed-task 'ps aux | grep sleep'",
        comment="Verify the renamed agent is still running its command",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 99999")
