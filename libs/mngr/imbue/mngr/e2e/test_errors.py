"""Tests for error handling in the mngr CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    # A valid agent type is supplied so the failure is genuinely attributable to
    # the unknown provider, not to an unrelated missing-type error that would be
    # raised first.
    result = e2e.run(
        "mngr create my-task --type command --provider nonexistent --no-connect --no-ensure-clean -- sleep 1",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()
    # The error must name the offending provider so the user knows what went wrong.
    expect(result.stderr).to_contain("nonexistent")

    # The failed create must not leave a half-registered agent behind.
    list_result = e2e.run("mngr list --provider local", comment="Confirm no agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This test creates a live local agent and then runs `mngr list`, which
# enumerates every configured provider; that discovery can exceed the default
# 10s per-test timeout when a remote provider (e.g. Docker) is unreachable and
# the client waits on a connection. Allow extra headroom for the verification.
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
    # The failure must be specifically the duplicate-name rejection, not some
    # unrelated error that also happens to exit non-zero.
    expect(duplicate_result.stderr).to_contain("already exists")

    # The rejected duplicate must leave the original agent untouched: exactly
    # one agent named "my-task" should remain.
    list_result = e2e.run("mngr list --format json", comment="Verify the original agent is intact")
    expect(list_result).to_succeed()
    agent_names = [agent["name"] for agent in json.loads(list_result.stdout)["agents"]]
    assert agent_names == ["my-task"], f"Expected only the original 'my-task', got {agent_names}"

    # The original agent must still be running its ORIGINAL command (sleep
    # 100099), proving the failed duplicate did not clobber or restart it with
    # the duplicate's command (sleep 100123).
    exec_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the original agent still runs its original command",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 100099")
    expect(exec_result.stdout).not_to_contain("sleep 100123")


@pytest.mark.release
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    # A concrete agent type is supplied so the command gets past type
    # resolution and actually reaches the clean-working-tree guard (the
    # tutorial relies on a configured default type that the isolated test
    # environment does not have). --no-connect avoids any connection attempt;
    # the command should abort before that anyway.
    result = e2e.run(
        "mngr create my-task --type command --no-connect -- true",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
    # Verify it failed for the *right* reason: the dirty working tree, not some
    # unrelated error (e.g. a missing default agent type). The guard aborts
    # before any host is resolved, so no agent is created.
    expect(result.stderr).to_contain("uncommitted changes")
    expect(result.stderr).to_contain("--no-ensure-clean")

    # The guard must abort cleanly: no agent should be registered. This proves
    # the failure happened before any agent was created, not midway through.
    list_result = e2e.run("mngr list --provider local", comment="Confirm no agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")
