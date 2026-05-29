"""Tests for error handling in the mngr CLI."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.timeout(120)
@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    # A valid --type is supplied so the command gets past agent-type resolution
    # and actually reaches provider resolution: this is what exercises the
    # invalid-provider failure path rather than the "no agent type" gate.
    result = e2e.run(
        "mngr create my-task --type command --provider nonexistent --no-connect --no-ensure-clean -- true",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()
    # The failure must specifically be about the unknown provider backend, not
    # some unrelated validation error.
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("nonexistent")
    expect(combined_output).to_match(r"(?i)provider")

    # The failed create must not leave a registered agent behind. Scope the
    # listing to the local provider so this does not trigger remote (Modal)
    # discovery, which would require @pytest.mark.modal.
    list_result = e2e.run("mngr list --provider local", comment="Confirm no agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


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
    # The duplicate must fail, and it must fail for the right reason: a name
    # collision. Assert on the error text so an unrelated failure (e.g. a crash)
    # cannot masquerade as the expected behavior. The message also points the
    # user at --reuse, which we verify is mentioned.
    expect(duplicate_result).to_fail()
    expect(duplicate_result.stderr).to_contain("already exists")
    expect(duplicate_result.stderr).to_contain("my-task")
    expect(duplicate_result.stderr).to_contain("--reuse")

    # The failed duplicate must leave the original agent untouched: it should
    # still be running its original command (sleep 100099), and the duplicate's
    # command (sleep 100123) must never have started.
    ps_result = e2e.run("mngr exec my-task 'ps -A -o args'", comment="Inspect processes in the original agent")
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain("sleep 100099")
    expect(ps_result.stdout).not_to_contain("sleep 100123")


@pytest.mark.release
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr create my-task --type command --no-connect -- true",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
    # The command must fail *because* the working tree is dirty, not for some
    # earlier reason (e.g. a missing default agent type). Assert on the specific
    # ensure-clean error so this test cannot silently pass for the wrong reason.
    expect(result.stderr).to_contain("uncommitted changes")
