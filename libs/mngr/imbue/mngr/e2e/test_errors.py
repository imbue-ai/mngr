"""Tests for error handling in the mngr CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    # A valid agent type is supplied so that the command gets past agent-type
    # resolution and actually reaches provider resolution; otherwise it would
    # fail for the unrelated reason of a missing default type.
    result = e2e.run(
        "mngr create my-task --type command --provider nonexistent --no-connect --no-ensure-clean -- sleep 100099",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()
    # The failure must be specifically about the unknown provider backend, not
    # some other validation error. Assert on the message naming the provider.
    expect(result.stderr).to_contain("nonexistent")
    expect(result.stderr).to_match(r"(?i)provider|backend")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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
    # The failure should clearly explain that the name is already taken.
    expect(duplicate_result.stderr).to_contain("already exists")

    # The duplicate attempt must not clobber or duplicate the original agent:
    # exactly one agent named "my-task" should remain.
    list_result = e2e.run("mngr list --format json", comment="Verify only the original agent exists")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, found {len(matching)}: {agents}"


@pytest.mark.release
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    # An explicit agent type is provided so the command reaches the working-tree
    # cleanliness check rather than aborting earlier on a missing default type.
    result = e2e.run(
        "mngr create my-task claude --no-connect",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
    # Verify the failure is specifically due to the dirty tree, and that the
    # error points the user at the escape hatch from the tutorial.
    expect(result.stderr).to_contain("uncommitted changes")
    expect(result.stderr).to_contain("--no-ensure-clean")
