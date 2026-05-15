"""Tests for error handling in the mngr CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    result = e2e.run(
        "mngr create my-task --provider nonexistent --no-connect --no-ensure-clean",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()
    # The error message must identify the bad provider and list valid ones so
    # users know how to recover. "local" is always registered.
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("nonexistent")
    expect(combined_output).to_contain("local")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_duplicate_name_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100099",
            comment="Create first agent",
        )
    ).to_succeed()

    duplicate_result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100123",
        comment="Attempt to create agent with duplicate name",
    )
    expect(duplicate_result).to_fail()
    expect(duplicate_result.stderr).to_contain("already exists")

    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="Verify the duplicate attempt did not affect the original agent",
    )
    expect(list_result).to_succeed()
    matching = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {matching}"
    # The surviving agent must still be the first one (sleep 100099), not the
    # rejected duplicate (sleep 100123).
    assert matching[0]["command"] == "sleep 100099", f"Unexpected command: {matching[0]['command']!r}"


@pytest.mark.release
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # by default, mngr aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mngr create my-task --no-ensure-clean
    # this is particularly useful when, for example, you are in the middle of a merge conflict and you just want the agent to finish it off
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """)
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr create my-task",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
    # Verify the failure is for the right reason (clean-tree check), not some
    # unrelated error. The error mentions uncommitted changes and points the
    # user at --no-ensure-clean as the documented workaround.
    expect(result.stderr).to_contain("uncommitted changes")
    expect(result.stderr).to_contain("--no-ensure-clean")
