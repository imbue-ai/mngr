"""Tests for destroying agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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
    # The piped IDs must actually reach destroy: each agent should be reported as
    # destroyed by the single command. This verifies the stdin plumbing worked
    # rather than just that the command exited 0 (which it would even on empty
    # input).
    expect(destroy_result.stdout).to_contain("Destroyed agent: agent-x")
    expect(destroy_result.stdout).to_contain("Destroyed agent: agent-y")
    # The summary line confirms the *count*: piping the full id list must tear
    # down exactly the two agents that existed, not a subset. This guards against
    # a regression where only the first piped id is consumed.
    expect(destroy_result.stdout).to_contain("Successfully destroyed 2 agent(s)")

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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_destroy_specific(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy a specific agent
        mngr destroy my-task
    """)
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


# No @pytest.mark.rsync: this test creates a local (`--type command`) agent with
# the default git-worktree transfer, which is an entirely local git operation.
# Local file provisioning copies in-process rather than shelling out to rsync
# (see imbue.mngr.hosts.file_upload: "rsync is unnecessary since writes are
# local"), so the test never invokes rsync and the resource guard would flag the
# mark as never-invoked.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr rm my-task
    """)
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_short_form_running_requires_force(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: without --force, the short-form
    # `rm` is safe by default and refuses to destroy a still-running agent even
    # after the confirmation prompt is answered "y".
    #
    # Unlike the happy-path siblings, this test deliberately never completes a
    # destroy: the running-agent guard rejects `rm` before any preservation runs,
    # and creating a local command agent uses a git worktree rather than an rsync
    # transfer. rsync is therefore never invoked here, so this test carries no
    # @pytest.mark.rsync (the resource guard fails a test that declares the mark
    # but never exercises it).
    e2e.write_tutorial_block("""
        # short form
        mngr rm my-task
    """)
    _create_my_task(e2e, 100608)
    refuse_result = e2e.run("yes | mngr rm my-task", comment="short form on a running agent")
    expect(refuse_result).to_succeed()
    expect(refuse_result.stdout).to_contain("Use --force to destroy running agents")

    # The agent must still be present since the destroy was refused.
    list_result = e2e.run("mngr list", comment="Verify the running agent was not destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_destroy_remove_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch
    """)
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

    # Branch deletion runs *after* the default post-destroy GC pass (see
    # cli/destroy.py), and that pass scans every provider -- including Modal --
    # over the network, which routinely takes longer than the default 30s
    # e2e.run timeout. Give it a generous timeout so the command can finish GC
    # and reach the branch-removal step it is being tested for.
    destroy_result = e2e.run(
        "mngr destroy my-task --force --remove-created-branch",
        comment="destroy and remove the created git branch",
        timeout=120.0,
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


@pytest.mark.release
@pytest.mark.tmux
# This test exercises only a local git-worktree agent: create writes the worktree
# locally, `mngr destroy --force --no-gc` does no file sync, and `mngr list
# --provider local` stays local -- none of which shell out to rsync (rsync is only
# used for remote hosts / non-git RSYNC transfers). The @pytest.mark.rsync mark
# carried by the other destroy tests is therefore omitted here; with it present the
# resource guard correctly flags "marked rsync but never invoked rsync".
# Unlike its sibling test_destroy_remove_branch (which only inspects `git branch`),
# this test also runs `mngr list` to confirm the agent is gone. Even with the
# remote-provider fan-out avoided (the destroy uses --no-gc and the list is scoped
# to --provider local), the create + destroy + list sequence is longer than the
# branch-only sibling; 120s matches the other create+list destroy tests
# (test_destroy_multiple_at_once) and leaves headroom for a slow agent create.
@pytest.mark.timeout(120)
def test_destroy_keeps_branch_by_default(e2e: E2eSession) -> None:
    # Companion to test_destroy_remove_branch for the same tutorial block: the
    # block's comment states that removing the branch is *not* the default
    # because losing the changes can be annoying, so the safe option is the
    # default. This test verifies that safe default -- a plain destroy (without
    # --remove-created-branch) leaves the agent's git branch intact.
    e2e.write_tutorial_block("""
        # destroy and also remove the git branch that was created for the agent
        # this is not the default because it can be annoying to lose the changes, so we default to the safe option
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100609)

    # The branch the default git-worktree transfer created must exist beforehand.
    branch_before = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch exists before destroy",
    )
    expect(branch_before).to_succeed()
    expect(branch_before.stdout).to_contain("mngr/my-task")

    # Destroy WITHOUT --remove-created-branch: the documented safe default.
    # --no-gc is an extra flag that skips the post-destroy garbage-collection
    # pass. That pass sweeps every provider (Modal, Docker) over the network and
    # is slow/flaky in the e2e environment, yet it is entirely orthogonal to the
    # branch-keeping behavior under test here (gc is already covered by
    # test_destroy_with_gc / test_destroy_no_gc). Skipping it keeps this test
    # fast and deterministic while still exercising the documented safe default.
    destroy_result = e2e.run(
        "mngr destroy my-task --force --no-gc",
        comment="destroy the agent but keep its git branch (the safe default)",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    # The safe default must not report deleting the branch.
    expect(destroy_result.stdout).not_to_contain("Deleted branch")

    # The agent is gone... Scope discovery to the local provider (the agent was
    # created locally): a bare `mngr list` fans out to every configured provider,
    # which is slow and, for a remote provider whose credentials are not set up in
    # the e2e environment (e.g. AWS), errors out and fails the command. The
    # `--provider local` scoping mirrors the pattern in test_config.py /
    # test_errors.py.
    list_result = e2e.run("mngr list --provider local", comment="verify the agent was destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")

    # ...but its branch is preserved, which is the whole point of the safe default.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's branch is preserved after a default destroy",
    )
    expect(branch_after).to_succeed()
    expect(branch_after.stdout).to_contain("mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_multiple_at_once(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy multiple agents at once
        mngr destroy agent-1 agent-2 agent-3 --force
    """)
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_multiple_aborts_on_unknown_agent(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: when one of the several names
    # does not match any agent, a forced `mngr destroy` reports the error and
    # destroys *nothing* -- the multi-target destroy is all-or-nothing rather
    # than partially applied (it would be surprising for `destroy a b c --force`
    # to silently tear down a and c when b is a typo).
    e2e.write_tutorial_block("""
        # destroy multiple agents at once
        mngr destroy agent-1 agent-2 agent-3 --force
    """)
    for name, sleep_value in [("agent-1", 100613), ("agent-2", 100614)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()

    # "no-such-agent" is a typo that matches nothing; it sits between two real
    # agents to prove the whole command aborts rather than destroying the names
    # that come before the bad one.
    destroy_result = e2e.run(
        "mngr destroy agent-1 no-such-agent agent-2 --force",
        comment="destroy multiple agents at once",
    )
    # The forced destroy reports the unmatched name and then destroys nothing
    # (it resets its target list to empty rather than partially applying).
    expect(destroy_result.stdout).to_contain("no-such-agent")
    expect(destroy_result.stdout).to_contain("No agents found to destroy")
    expect(destroy_result.stdout).not_to_contain("Destroyed agent")

    # Both real agents must still be present, confirming nothing was torn down.
    list_result = e2e.run("mngr list", comment="verify no agents were destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("agent-1")
    expect(list_result.stdout).to_contain("agent-2")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # to preview what would be destroyed without doing it, run without --force and answer "no" at the prompt
        mngr destroy my-task
    """)
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_with_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy and run garbage collection afterward (this is the default)
        mngr destroy my-task --force --gc
    """)
    _create_my_task(e2e, 100607)
    # The post-destroy gc pass queries every configured provider (including the
    # remote modal provider in this e2e environment), which routinely takes
    # longer than the 30s default e2e.run timeout. Give it a generous budget
    # within the test's 120s timeout so the documented `--gc` flow can finish.
    destroy_result = e2e.run(
        "mngr destroy my-task --force --gc",
        comment="destroy and run garbage collection afterward",
        timeout=90.0,
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


# No @pytest.mark.rsync: this test creates a localhost command agent (a git
# worktree, not a file transfer) and destroys it, so it never invokes rsync. The
# resource guard fails an otherwise-passing test that is marked rsync but never
# uses it ("marked rsync but never invoked rsync").
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_destroy_no_gc(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # by default, gc (garbage collection) runs after destroying any agent
        # you can disable this if you want:
        mngr destroy --no-gc
        # however, note that it is generally a good idea to ensure that "mngr gc" is run periodically,
        # otherwise resources (ex: worktrees, hosts, containers, volumes, etc) will accumulate over time
    """)
    # The tutorial shows `mngr destroy --no-gc` in isolation to demonstrate the flag,
    # but a real invocation needs an agent to destroy. Create one, destroy it with
    # --no-gc, and verify both that the agent is gone AND that the post-destroy
    # garbage-collection pass did not run (its progress output must be absent).
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

    # Scope the verification listing to the local provider: the agent was created
    # with the default local provider (`--type command`, no `--provider`), so the
    # local listing is sufficient to confirm it is gone. A plain `mngr list`
    # enumerates every enabled provider and fails if any remote provider (e.g.
    # Docker) is unreachable, which the e2e environment does not guarantee -- see
    # the same `--provider local` pattern in e2e/test_errors.py.
    list_result = e2e.run("mngr list --provider local", comment="verify the agent was destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_destroy_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy has a special variant for finding an agent by its tmux session name:
        mngr destroy --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)
    """)
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


# NOTE: no @pytest.mark.rsync. This test only creates a *local* agent (default
# provider) and targets it by its local tmux session name. A same-host local git
# repo transfers via git-worktree, and rsync only ever runs for local<->remote
# transfers, so this test never invokes rsync (the resource guard rejects a
# superfluous rsync mark on a passing test).
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_by_session_name_happy_path(e2e: E2eSession) -> None:
    # Shares the same tutorial block as test_destroy_by_session_name: that test
    # covers the unhappy path (a bogus session name), while this one covers the
    # happy path where the session name maps to a real agent that gets destroyed.
    e2e.write_tutorial_block("""
        # destroy has a special variant for finding an agent by its tmux session name:
        mngr destroy --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-q)
    """)
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

    # Scope the verification listing to the local provider (where the agent
    # lives -- it was created on the default local provider and targeted by its
    # local tmux session name). A bare `mngr list` enumerates every enabled
    # provider and exits non-zero if any is unreachable (per error_handling.md);
    # in this checkout the unconfigured cloud-provider plugins (e.g. aws) are
    # enabled-by-default and report as unreachable, which is unrelated to whether
    # the agent was destroyed.
    list_result = e2e.run("mngr list --provider local", comment="verify the agent was actually destroyed")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")
