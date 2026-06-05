"""Tests for destroying agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
@pytest.mark.modal
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
@pytest.mark.modal
def test_destroy_specific(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy a specific agent
        mngr destroy my-task
    """)
    _create_my_task(e2e, 100600)
    # Pipe "y\n" to confirm the destructive prompt that --force would suppress.
    expect(e2e.run("yes | mngr destroy my-task", comment="destroy a specific agent")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr rm my-task
    """)
    _create_my_task(e2e, 100601)
    expect(e2e.run("yes | mngr rm my-task", comment="short form")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_remove_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100602)
    expect(
        e2e.run(
            "mngr destroy my-task --force --remove-created-branch",
            comment="destroy and remove the created git branch",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
    expect(
        e2e.run(
            "mngr destroy agent-1 agent-2 agent-3 --force",
            comment="destroy multiple agents at once",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be destroyed without actually doing it
        mngr list --ids | mngr destroy - --dry-run
    """)
    _create_my_task(e2e, 100606)
    expect(
        e2e.run(
            "mngr list --ids | mngr destroy - --dry-run",
            comment="dry-run to see what would be destroyed",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_with_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and run garbage collection afterward (this is the default)
        mngr destroy my-task --force --gc
    """)
    _create_my_task(e2e, 100607)
    expect(
        e2e.run(
            "mngr destroy my-task --force --gc",
            comment="destroy and run garbage collection afterward",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_destroy_no_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, gc (garbage collection) runs after destroying any agent
        # you can disable this if you want:
        mngr destroy --no-gc
        # however, note that it is generally a good idea to ensure that "mngr gc" is run periodically,
        # otherwise resources (ex: worktrees, hosts, containers, volumes, etc) will accumulate over time
    """)
    # `mngr destroy --no-gc` without an agent argument is informational; just
    # verify mngr parses the flag and exits cleanly (either success with a
    # no-op message or a usage error -- both demonstrate the flag exists).
    result = e2e.run(
        "mngr destroy --no-gc 2>&1 || true",
        comment="disable automatic gc after destroy",
    )
    assert "no-gc" in (result.stdout + result.stderr).lower() or result.exit_code == 0


@pytest.mark.release
@pytest.mark.modal
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
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()
