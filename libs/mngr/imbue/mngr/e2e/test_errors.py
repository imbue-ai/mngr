"""Tests for error handling in the mngr CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    result = e2e.run(
        "mngr create my-task --type command --provider nonexistent --no-connect --no-ensure-clean -- echo hi",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()
    # The failure must be due to the unknown provider, not some earlier check
    # (e.g. missing --type). Without these assertions, the test would have
    # passed even when the command failed for an unrelated reason.
    expect(result.stderr).to_contain("nonexistent")
    expect(result.stderr).to_contain("Unknown provider backend")


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
    # The failure must be reported as a duplicate-name conflict, not some
    # other create-time error.
    expect(duplicate_result.stdout + duplicate_result.stderr).to_contain("already exists")

    # The original agent must be untouched: only one agent named 'my-task',
    # and its command is still the first sleep value, not the duplicate's.
    list_result = e2e.run(
        "mngr list --format json",
        comment="Confirm the duplicate attempt did not replace the existing agent",
    )
    expect(list_result).to_succeed()
    agents = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(agents) == 1, f"Expected exactly one 'my-task' agent, got {agents}"
    assert agents[0]["command"] == "sleep 100099", (
        f"Existing agent's command should be unchanged; got {agents[0]['command']!r}"
    )


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr create my-task --type command --no-connect -- sleep 100124",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
    # Verify the failure is specifically due to the dirty tree check,
    # not some unrelated validation error.
    combined = (result.stdout + result.stderr).lower()
    assert "uncommitted" in combined or "ensure-clean" in combined, (
        f"Expected dirty tree error. stderr: {result.stderr}\nstdout: {result.stdout}"
    )
