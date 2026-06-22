"""Tests for error handling in the mngr CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
# The `mngr create` subprocess cold-starts and imports every registered
# provider backend SDK (aws, azure, gcp, modal, ...) before it can report that
# the requested provider is unknown. That import cost alone pushes the single
# command just past the default 10s per-test timeout (~10.5s observed), so give
# it headroom; the command itself still fails fast once loaded.
@pytest.mark.timeout(120)
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


@pytest.mark.release
@pytest.mark.tmux
# This test creates a live local agent and then exercises several verification
# commands (`mngr list`, `mngr exec`) against it. Bringing up the local agent
# and running these can exceed the default 10s per-test timeout, so allow extra
# headroom for the verification.
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
    # one agent named "my-task" should remain. Scope to the local provider so
    # discovery of unconfigured remote providers (e.g. AWS) cannot make `list`
    # exit non-zero; the agent was created on the local provider anyway.
    list_result = e2e.run(
        "mngr list --provider local --format json", comment="Verify the original agent is intact"
    )
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
# Each `mngr` invocation pays a multi-second cold-start (process spawn plus
# module import) before it does any work, and this test issues two of them in
# sequence (the rejected `create` and the follow-up `list`). On a cold or slow
# host that comfortably exceeds the default 10s per-test timeout even though
# the dirty-tree guard itself aborts almost immediately. Allow extra headroom.
@pytest.mark.timeout(60)
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Creates a live local agent and then runs `mngr list`/`mngr exec`; each `mngr`
# invocation pays a multi-second cold-start, so the full sequence comfortably
# exceeds the default 10s per-test timeout even though no single step is slow.
@pytest.mark.timeout(120)
def test_create_with_dirty_tree_succeeds_with_no_ensure_clean(e2e: E2eSession) -> None:
    # Happy-path complement to test_create_with_dirty_tree_fails: the tutorial
    # documents `--no-ensure-clean` as the escape hatch for a dirty tree, so
    # creating in a dirty tree with that flag must succeed (and carry the
    # uncommitted change over to the agent's work dir).
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    # --type command keeps the agent type explicit (the isolated environment has
    # no configured default), and --no-connect avoids a connection attempt. The
    # bypass flag is the point of the test.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --no-connect --provider local -- sleep 300",
        comment="Create with --no-ensure-clean in a dirty tree",
    )
    expect(result).to_succeed()

    # The agent must actually exist now (the dirty tree did not block creation).
    list_result = e2e.run("mngr list --provider local", comment="Confirm the agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the concrete effect of --no-ensure-clean: the uncommitted file is
    # carried into the running agent's work dir, not silently dropped. Asserting
    # on the agent's own filesystem proves the agent is real and running, not
    # merely registered.
    exec_result = e2e.run(
        "mngr exec my-task 'ls dirty.txt'",
        comment="Confirm the uncommitted change reached the agent's work dir",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("dirty.txt")
