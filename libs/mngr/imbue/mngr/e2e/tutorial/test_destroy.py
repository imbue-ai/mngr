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
@pytest.mark.timeout(60)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy without confirmation prompt
        mngr destroy my-task --force

    Scope: `mngr destroy --force` skips the confirmation prompt and actually
    destroys the named agent -- it reports "Destroyed agent: my-task" and the
    agent (confirmed present beforehand) no longer appears in `mngr list`.
    """
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100098",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    # Confirm the agent really exists before we destroy it, so the after-check
    # below is a meaningful before/after contrast rather than a vacuous one.
    list_before = e2e.run("mngr list", comment="Verify the agent exists before destroy")
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
@pytest.mark.timeout(120)
def test_destroy_all_via_stdin(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy all agents (be careful!)
        mngr list --ids | mngr destroy - --force

    Scope: piping `mngr list --ids` into `mngr destroy - --force` reads agent
    ids from stdin and destroys every one of them in a single command -- each
    agent is reported as destroyed and none remain in `mngr list` afterward. The
    stdin plumbing must actually carry the ids (an empty stdin would exit 0 too).
    """
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
    # The piped IDs must actually reach destroy: each agent should be reported as
    # destroyed by the single command. This verifies the stdin plumbing worked
    # rather than just that the command exited 0 (which it would even on empty
    # input).
    expect(destroy_result.stdout).to_contain("Destroyed agent: agent-x")
    expect(destroy_result.stdout).to_contain("Destroyed agent: agent-y")

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
@pytest.mark.timeout(180)
def test_destroy_specific(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy a specific agent
        mngr destroy my-task

    Scope: the bare `mngr destroy my-task` (no --force) prompts for
    confirmation; answering "y" destroys the named agent (reported as "Destroyed
    agent: my-task") and it no longer appears in `mngr list`. The agent is
    stopped first because a non-forced destroy refuses a running agent.
    """
    _create_my_task(e2e, 100600)
    # The bare `mngr destroy` (no --force) refuses to destroy a *running* agent,
    # so stop it first. This lets the documented command actually destroy the
    # agent rather than short-circuiting on the running-agent guard.
    expect(e2e.run("mngr stop my-task", comment="stop the agent before destroying it", timeout=60.0)).to_succeed()
    # Pipe "y\n" to confirm the destructive prompt that --force would suppress.
    destroy_result = e2e.run("yes | mngr destroy my-task", comment="destroy a specific agent", timeout=90.0)
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # Verify the agent is actually gone, not just that the command exited 0.
    list_result = e2e.run("mngr list", comment="verify the agent no longer exists")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_short_form(e2e: E2eSession) -> None:
    """Tutorial block:
        # short form
        mngr rm my-task

    Scope: `mngr rm` is an alias for `mngr destroy`. `mngr rm my-task --force`
    performs a real removal -- it reports "Destroyed agent: my-task" and the
    agent no longer appears in `mngr list`. (--force is needed because the agent
    is still running; see the companion test for the non-forced refusal.)
    """
    _create_my_task(e2e, 100601)
    # `--force` is an extra flag (the agent created above is still running, and a
    # non-forced destroy refuses running agents -- see the companion test below).
    # It also lets us verify that the `rm` alias performs a real removal.
    destroy_result = e2e.run("mngr rm my-task --force", comment="short form")
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")

    # `rm` is an alias for `destroy`, so the agent must actually be gone afterward.
    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_short_form_running_requires_force(e2e: E2eSession) -> None:
    """Tutorial block:
        # short form
        mngr rm my-task

    Scope: the unhappy path of the same block -- without --force the short-form
    `mngr rm` is safe by default and refuses to destroy a still-running agent
    even when the confirmation prompt is answered "y" ("Use --force to destroy
    running agents"); the agent remains present in `mngr list`.
    """
    _create_my_task(e2e, 100608)
    refuse_result = e2e.run("yes | mngr rm my-task", comment="short form on a running agent")
    expect(refuse_result).to_succeed()
    expect(refuse_result.stdout).to_contain("Use --force to destroy running agents")

    # The agent must still be present since the destroy was refused.
    list_result = e2e.run("mngr list", comment="Verify the running agent was not destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_remove_branch(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch

    Scope: `--remove-created-branch` additionally deletes the git branch
    (mngr/my-task) that was created for the agent -- destroy reports both
    "Destroyed agent: my-task" and "Deleted branch: mngr/my-task", and the
    branch (confirmed present beforehand) is gone afterward.
    """
    _create_my_task(e2e, 100602)

    # The default git-worktree transfer creates a branch named after the agent.
    # Confirm it exists before we ask destroy to remove it, so the after-check
    # is a meaningful before/after contrast rather than a vacuous assertion.
    branch_before = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch exists before destroy",
    )
    expect(branch_before).to_succeed()
    expect(branch_before.stdout).to_contain("mngr/my-task")

    destroy_result = e2e.run(
        "mngr destroy my-task --force --remove-created-branch",
        comment="destroy and remove the created git branch",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    expect(destroy_result.stdout).to_contain("Deleted branch: mngr/my-task")

    # The branch the agent was created on must be gone now -- this is the whole
    # point of --remove-created-branch.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch was removed after destroy",
    )
    expect(branch_after).to_succeed()
    expect(branch_after.stdout).not_to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_keeps_branch_by_default(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch

    Scope: the safe default asserted by the block's comment (companion to
    test_destroy_remove_branch) -- a plain destroy *without*
    --remove-created-branch destroys the agent (gone from `mngr list`) but does
    not report "Deleted branch" and leaves the agent's git branch (mngr/my-task)
    intact.
    """
    _create_my_task(e2e, 100609)

    # The branch the default git-worktree transfer created must exist beforehand.
    branch_before = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch exists before destroy",
    )
    expect(branch_before).to_succeed()
    expect(branch_before.stdout).to_contain("mngr/my-task")

    # Destroy WITHOUT --remove-created-branch: the documented safe default.
    destroy_result = e2e.run(
        "mngr destroy my-task --force",
        comment="destroy the agent but keep its git branch (the safe default)",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # The safe default must not report deleting the branch.
    expect(destroy_result.stdout).not_to_contain("Deleted branch")

    # The agent is gone...
    list_result = e2e.run("mngr list", comment="verify the agent was destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")

    # ...but its branch is preserved, which is the whole point of the safe default.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch is preserved after a default destroy",
    )
    expect(branch_after).to_succeed()
    expect(branch_after.stdout).to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_multiple_at_once(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy multiple agents at once
        mngr destroy agent-1 agent-2 agent-3 --force

    Scope: a single `mngr destroy` with several agent names tears down all of
    them at once -- each is reported as destroyed, the summary confirms the count
    ("Successfully destroyed 3 agent(s)"), and none remain in `mngr list`.
    """
    agent_names = ["agent-1", "agent-2", "agent-3"]
    for name, sleep_value in zip(agent_names, [100603, 100604, 100605], strict=True):
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()

    list_before = e2e.run("mngr list", comment="Verify all three agents exist before destroy")
    expect(list_before).to_succeed()
    for name in agent_names:
        expect(list_before.stdout).to_contain(name)

    destroy_result = e2e.run(
        "mngr destroy agent-1 agent-2 agent-3 --force",
        comment="destroy multiple agents at once",
    )
    expect(destroy_result).to_succeed()
    # Each named agent should be reported as destroyed by the single command.
    for name in agent_names:
        expect(destroy_result.stdout).to_contain(f"Destroyed agent: {name}")
    # The summary line confirms the *count* -- the whole point of "destroy
    # multiple at once" is that one command tears down all three, so the command
    # must report destroying exactly three agents (not, e.g., just the first one).
    expect(destroy_result.stdout).to_contain("Successfully destroyed 3 agent(s)")

    list_after = e2e.run("mngr list", comment="Verify none of the agents remain after destroy")
    expect(list_after).to_succeed()
    for name in agent_names:
        expect(list_after.stdout).not_to_contain(name)


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_dry_run(e2e: E2eSession) -> None:
    """Tutorial block:
        # to preview what would be destroyed without doing it, run without --force and answer "no" at the prompt
        mngr destroy my-task

    Scope: running `mngr destroy` without --force previews what *would* be
    destroyed (listing the agent, "will be destroyed") and prompts for
    confirmation; answering "no" aborts with a non-zero exit and destroys nothing
    -- the agent still exists. This confirmation preview is the supported
    replacement for the removed --dry-run flag on multi-target commands.
    """
    _create_my_task(e2e, 100606)
    # Running destroy without --force lists what *would* be destroyed and then
    # prompts for confirmation. Answering "no" aborts without destroying
    # anything -- this confirmation preview is the supported replacement for the
    # old --dry-run flag, which was removed from multi-target commands.
    preview_result = e2e.run(
        "echo n | mngr destroy my-task",
        comment="preview what would be destroyed, then abort at the prompt",
    )
    # Aborting at the confirmation prompt exits non-zero.
    expect(preview_result).to_fail()
    # The preview lists the agent that would be destroyed.
    expect(preview_result.stdout).to_contain("will be destroyed")
    expect(preview_result.stdout).to_contain("my-task")
    # Crucially, nothing was actually destroyed: the agent still exists.
    list_result = e2e.run("mngr list", comment="verify the agent was not destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_with_gc(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy and run garbage collection afterward (this is the default)
        mngr destroy my-task --force --gc

    Scope: `--gc` runs garbage collection after the destroy -- the agent is
    destroyed (gone from `mngr list`) and the output shows the gc pass ("Garbage
    collecting"), which is what distinguishes it from a plain destroy.
    """
    _create_my_task(e2e, 100607)
    destroy_result = e2e.run(
        "mngr destroy my-task --force --gc",
        comment="destroy and run garbage collection afterward",
    )
    expect(destroy_result).to_succeed()
    # The agent itself must be destroyed.
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # The whole point of --gc is that garbage collection runs afterward, so the
    # output must show the gc pass (this is what distinguishes it from a plain
    # destroy).
    expect(destroy_result.stdout).to_contain("Garbage collecting")

    # The agent must no longer appear in the listing after being destroyed.
    list_result = e2e.run("mngr list", comment="verify my-task is gone after destroy")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_no_gc(e2e: E2eSession) -> None:
    """Tutorial block:
        # by default, gc (garbage collection) runs after destroying any agent
        # you can disable this if you want:
        mngr destroy --no-gc
        # however, note that it is generally a good idea to ensure that "mngr gc" is run periodically,
        # otherwise resources (ex: worktrees, hosts, containers, volumes, etc) will accumulate over time

    Scope: `--no-gc` disables the post-destroy garbage-collection pass -- the
    agent is still destroyed (gone from `mngr list`) but the "Garbage
    collecting" progress line is absent, which is what proves the flag took
    effect. (The block shows the flag in isolation; a real invocation needs an
    agent to destroy.)
    """
    _create_my_task(e2e, 100608)
    destroy_result = e2e.run(
        "mngr destroy my-task --no-gc --force",
        comment="disable automatic gc after destroy",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # With gc disabled, the "Garbage collecting..." progress line emitted by the
    # post-destroy gc pass must not appear -- this is what proves --no-gc took effect.
    expect(destroy_result.stdout).not_to_contain("Garbage collecting")

    list_result = e2e.run("mngr list", comment="verify the agent was destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.release
def test_destroy_by_session_name(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy has a special variant for finding an agent by its tmux session name:
        mngr destroy --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)

    Scope: the unhappy path of the --session variant -- a name that does not
    start with the configured tmux session prefix cannot be mapped to an agent,
    so `mngr destroy --session` exits non-zero with an error explaining the
    session-prefix requirement ("does not match the expected format") rather than
    crashing. It fails before reaching any agent, so it never exercises modal.
    """
    # Unhappy path: "my-session-name" does not start with the configured tmux
    # session prefix, so mngr cannot derive an agent name from it and exits with
    # an error instead of crashing. Because it fails before reaching any agent,
    # it does not exercise modal (hence no @pytest.mark.modal).
    result = e2e.run(
        "mngr destroy --session my-session-name",
        comment="destroy variant that finds an agent by tmux session name",
    )
    assert result.exit_code != 0, f"expected a non-zero exit code, transcript:\n{e2e.transcript}"
    combined_output = (result.stdout + result.stderr).lower()
    assert "does not match the expected format" in combined_output, (
        f"expected an error explaining the session-prefix requirement, transcript:\n{e2e.transcript}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_by_session_name_happy_path(e2e: E2eSession) -> None:
    """Tutorial block:
        # destroy has a special variant for finding an agent by its tmux session name:
        mngr destroy --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)

    Scope: the happy path of the --session variant (companion to
    test_destroy_by_session_name) -- when the session name maps to a real agent
    (its tmux session is "{prefix}{agent_name}"), `mngr destroy --session ...
    --force` destroys that agent ("Destroyed agent: my-task") and it no longer
    appears in `mngr list`.
    """
    _create_my_task(e2e, 100608)

    # An agent's tmux session name is "{prefix}{agent_name}" (see
    # base_agent.BaseAgent.session_name). Read the configured prefix from the
    # environment rather than hardcoding it, then reconstruct my-task's session
    # name so we can target it the same way the ctrl-q hotkey does.
    prefix_result = e2e.run('printf %s "$MNGR_PREFIX"', comment="read the configured tmux session prefix")
    expect(prefix_result).to_succeed()
    prefix = prefix_result.stdout.strip()
    assert prefix, f"expected MNGR_PREFIX to be set, transcript:\n{e2e.transcript}"
    session_name = f"{prefix}my-task"

    destroy_result = e2e.run(
        f"mngr destroy --session {session_name} --force",
        comment="destroy the agent found by its tmux session name",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")

    list_result = e2e.run("mngr list", comment="verify the agent was actually destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")
