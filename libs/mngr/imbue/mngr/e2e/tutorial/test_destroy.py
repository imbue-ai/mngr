"""Tests for destroying agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Per-command timeout (seconds) for individual Modal operations. The default
# 30s session timeout is too short for provisioning/tearing down a remote host.
_REMOTE_TIMEOUT = 120.0


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # destroy without confirmation prompt
    mngr destroy my-task --force
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100098",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    # Confirm the agent exists before destroying it, so the post-destroy
    # absence check below proves destroy removed it (rather than create
    # having silently failed to make it in the first place).
    list_before = e2e.run("mngr list", comment="Verify agent exists before destroy")
    expect(list_before).to_succeed()
    expect(list_before.stdout).to_contain("my-task")

    destroy_result = e2e.run(
        "mngr destroy my-task --force",
        comment="destroy without confirmation prompt",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")

    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_destroy_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all agents (be careful!)
        mngr list --ids | mngr destroy - --force
    """)
    for name, sleep_seconds in [("agent-x", 100102), ("agent-y", 100120)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"Create {name}",
            )
        ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify both agents exist")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("agent-x")
    expect(list_result.stdout).to_contain("agent-y")

    destroy_result = e2e.run(
        "mngr list --ids | mngr destroy - --force",
        comment="Destroy all agents via stdin piping",
    )
    expect(destroy_result).to_succeed()
    # The piped ids must have driven both destroys, not just emptied the list
    # some other way. Assert both names appear in the destroy output and that
    # the summary reports the full count.
    expect(destroy_result.stdout).to_contain("agent-x")
    expect(destroy_result.stdout).to_contain("agent-y")
    expect(destroy_result.stdout).to_contain("2 agent(s)")

    list_after = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("No agents found")


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_specific(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy a specific agent
        mngr destroy my-task
    """)
    _create_my_task(e2e, 100600)
    # `mngr destroy` without --force refuses to destroy a *running* agent, so stop
    # it first. This isolates the confirmation-prompt path that this tutorial block
    # demonstrates from the separate "use --force to destroy running agents" guard.
    expect(e2e.run("mngr stop my-task", comment="stop the agent before destroying")).to_succeed()
    # Pipe "y\n" to confirm the destructive prompt that --force would suppress.
    destroy_result = e2e.run("yes | mngr destroy my-task", comment="destroy a specific agent")
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # Verify the agent is actually gone.
    list_result = e2e.run("mngr list", comment="verify the agent no longer exists")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_destroy_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr rm my-task
    """)
    _create_my_task(e2e, 100601)
    # A bare `mngr rm`/`mngr destroy` (no --force) refuses to destroy a running
    # agent, so stop it first; this lets the tutorial command actually destroy it.
    expect(e2e.run("mngr stop my-task", comment="stop my-task before destroying")).to_succeed()
    # Pipe "y\n" to confirm the destructive prompt that --force would suppress.
    rm_result = e2e.run("yes | mngr rm my-task", comment="short form", timeout=120.0)
    expect(rm_result).to_succeed()
    expect(rm_result.stdout).to_contain("Destroyed agent: my-task")
    # Verify the agent is actually gone, not just that the command exited 0.
    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_remove_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100602)
    # Creating the agent makes a git branch named after it; confirm it exists so
    # the post-destroy check that it is gone is meaningful.
    branches_before = e2e.run("git branch --list 'mngr/my-task'", comment="confirm the agent branch exists")
    expect(branches_before).to_succeed()
    expect(branches_before.stdout).to_contain("mngr/my-task")

    destroy_result = e2e.run(
        "mngr destroy my-task --force --remove-created-branch",
        comment="destroy and remove the created git branch",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # --remove-created-branch must actually delete the branch that create made.
    expect(destroy_result.stdout).to_contain("Deleted branch: mngr/my-task")

    # Verify the concrete effects: the agent is gone and the branch was removed.
    list_result = e2e.run("mngr list", comment="verify the agent no longer appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")

    branches_after = e2e.run("git branch --list 'mngr/my-task'", comment="verify the agent branch was removed")
    expect(branches_after).to_succeed()
    expect(branches_after.stdout).not_to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_multiple_at_once(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy multiple agents at once
        mngr destroy agent-1 agent-2 agent-3 --force
    """)
    for name, sleep_value in [("agent-1", 100603), ("agent-2", 100604), ("agent-3", 100605)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()
    destroy_result = e2e.run(
        "mngr destroy agent-1 agent-2 agent-3 --force",
        comment="destroy multiple agents at once",
    )
    expect(destroy_result).to_succeed()
    # Each named agent must be reported as destroyed (the command processes them
    # in an unspecified order, so assert on presence rather than ordering).
    for name in ("agent-1", "agent-2", "agent-3"):
        expect(destroy_result.stdout).to_contain(f"Destroyed agent: {name}")
    expect(destroy_result.stdout).to_contain("Successfully destroyed 3 agent(s)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # to preview what would be destroyed, list the agents first (destroy composes with stdin)
        mngr list --ids
    """)
    _create_my_task(e2e, 100606)

    # Capture the agent id so we can confirm the preview command surfaces it.
    ids_result = e2e.run("mngr list --ids", comment="preview what would be destroyed")
    expect(ids_result).to_succeed()
    listed_ids = [line for line in ids_result.stdout.splitlines() if line.strip()]
    assert len(listed_ids) == 1, f"Expected exactly one agent id from `mngr list --ids`, got: {ids_result.stdout!r}"

    # Previewing must NOT destroy anything: the agent must still exist afterward.
    list_after = e2e.run("mngr list", comment="verify the agent still exists after previewing")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(90)
def test_destroy_with_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and run garbage collection afterward (this is the default)
        mngr destroy my-task --force --gc
    """)
    _create_my_task(e2e, 100607)
    destroy_result = e2e.run(
        "mngr destroy my-task --force --gc",
        comment="destroy and run garbage collection afterward",
    )
    expect(destroy_result).to_succeed()
    # The agent itself must actually be destroyed...
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # ...and garbage collection must actually run afterward (the point of --gc).
    expect(destroy_result.stdout).to_contain("Garbage collecting")

    # Verify the concrete effect: the agent no longer exists.
    list_result = e2e.run("mngr list", comment="Verify the agent is gone after destroy")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(360)
def test_destroy_no_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, gc (garbage collection) runs after destroying any agent
        # you can disable this if you want:
        mngr destroy --no-gc
        # however, note that it is generally a good idea to ensure that "mngr gc" is run periodically,
        # otherwise resources (ex: worktrees, hosts, containers, volumes, etc) will accumulate over time
    """)
    # The tutorial block elides the agent name to keep the focus on the --no-gc
    # flag. Run it against a real Modal agent (plus --force to skip the prompt)
    # so the destroy actually tears down a remote host -- this is the only way
    # to exercise the destroy-without-gc path against real resources, since
    # --no-gc deliberately skips the post-destroy gc pass that would otherwise
    # be what contacts Modal.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-ensure-clean --no-connect -- sleep 100608",
            comment="create a Modal agent to be destroyed",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()
    result = e2e.run(
        "mngr destroy my-task --force --no-gc",
        comment="disable automatic gc after destroy",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Destroyed agent: my-task")
    # The defining behavior of --no-gc: the post-destroy garbage collection pass
    # (which would otherwise print "Garbage collecting...") must not run.
    expect(result.stdout).not_to_contain("Garbage collecting")
    # The agent itself is still destroyed regardless of gc.
    list_after = e2e.run("mngr list", comment="verify the agent was destroyed", timeout=_REMOTE_TIMEOUT)
    expect(list_after).to_succeed()
    expect(list_after.stdout).not_to_contain("my-task")


# No @pytest.mark.modal: the tutorial's literal session name does not match the
# configured test prefix, so destroy fails at input validation before any
# provider (modal) code runs. The modal mark would otherwise be flagged as
# superfluous by the resource guard.
@pytest.mark.release
def test_destroy_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy has a special variant for finding an agent by its tmux session name:
        mngr destroy --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)
    """)
    result = e2e.run(
        "mngr destroy --session my-session-name",
        comment="destroy variant that finds an agent by tmux session name",
    )
    # The literal session name "my-session-name" lacks the configured prefix, so
    # destroy rejects it as malformed rather than reaching any provider. Assert on
    # the specific validation error so the test cannot silently pass on an
    # unrelated non-zero exit.
    assert result.exit_code != 0
    assert "does not match the expected format" in (result.stdout + result.stderr).lower()
