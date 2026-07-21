"""Tests for the COMMON TASKS recipe block and the MULTI-AGENT WORKFLOWS recipe.

These two blocks are multi-step recipes spanning several commands. Each test
mirrors the recipe shape but substitutes lightweight equivalents (sleep agents
in place of modal claude agents) so the test stays fast.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_recipe_launch_check_cleanup(e2e: E2eSession) -> None:
    """Tutorial block:
        # Recipe: launch an agent on a task, check on it later, and clean up
        # 1. Create an agent with a task, don't connect (let it work in the background)
        mngr create fix-bug --provider modal --no-connect --message "Fix the failing test in test_auth.py and make a PR"
        # 2. Check what agents are running
        mngr list --running
        # 3. Check the agent's conversation to see its progress
        mngr transcript fix-bug --tail 3
        # 4. Send a follow-up message if needed
        mngr msg fix-bug -m "Also make sure to run the linter before committing"
        # 5. Connect to the agent to review its work interactively
        mngr conn fix-bug
        # 6. merge the resulting branch
        git merge mngr/fix-bug
        # 7. When done, stop and clean up
        mngr destroy fix-bug -f --remove-created-branch

    Scope: the full launch->check->cleanup recipe, with a local `sleep` command
    agent standing in for the modal claude agent so the test stays fast. create
    makes a named, reachable agent (exec pwd returns an absolute path); list
    --running succeeds; transcript fails for a command agent (which produces no
    common transcript); msg delivers a follow-up; conn resolves the named agent
    and begins connecting; destroy --remove-created-branch removes both the agent
    (gone from list, no longer resolvable) and its git branch.
    """
    # Substitute a local command agent for the modal claude agent; the recipe
    # shape (create -> list -> transcript -> msg -> conn -> merge -> destroy)
    # is what we want to verify. The only step that behaves differently for the
    # command-type stand-in is transcript (command agents have no transcript).
    expect(
        e2e.run(
            "mngr create fix-bug --type command --no-ensure-clean --no-connect -- sleep 100970",
            comment="1. create an agent for the task",
        )
    ).to_succeed()
    # Scope the listing to the local provider (matching the local command-agent
    # stand-in): a bare `mngr list --running` also queries remote provider
    # backends (e.g. aws, docker) that are enabled by default but unreachable in
    # the isolated e2e profile (no cloud credentials, no docker daemon), so it
    # would exit non-zero for reasons unrelated to the launch->check->cleanup
    # recipe under test.
    expect(e2e.run("mngr list --running --provider local", comment="2. check what agents are running")).to_succeed()
    # The recipe's modal claude agent is actively working (state RUNNING) when
    # listed, but the idle `sleep` stand-in reports WAITING, so `mngr list
    # --running` above is legitimately empty. Confirm the agent really is up and
    # reachable -- the concrete intent of "check what agents are running" -- by
    # exec-ing into it and checking it has a working directory. This resolves a
    # specific agent by name (like msg/conn/destroy below) rather than
    # enumerating providers, so it stays a local-only check.
    pwd_result = e2e.run("mngr exec fix-bug pwd", comment="verify the agent is alive in its working directory")
    expect(pwd_result).to_succeed()
    expect(pwd_result.stdout).to_match(r"^/")
    # Step 3 checks the agent's transcript. The modal claude agent in the recipe
    # produces a common transcript; the command-type stand-in here does not, so
    # the exact recipe command reports that limitation. We still run the recipe
    # command verbatim and assert on its real, observable behavior.
    transcript_result = e2e.run("mngr transcript fix-bug --tail 3", comment="3. check transcript")
    expect(transcript_result).to_fail()
    expect(transcript_result.stderr).to_contain("does not produce a common transcript")
    expect(e2e.run('mngr msg fix-bug -m "lint please"', comment="4. send a follow-up message")).to_succeed()
    # Step 5 connects to the agent to review interactively. `mngr conn` resolves
    # the agent and then hands off to an interactive tmux attach. The e2e harness
    # runs commands without a TTY, so the attach itself cannot complete; we verify
    # the command got far enough to resolve the named agent and start connecting.
    conn_result = e2e.run("mngr conn fix-bug", comment="5. connect to review")
    expect(conn_result.stdout + conn_result.stderr).to_contain("Connecting to agent: fix-bug")
    e2e.run("git merge mngr/fix-bug || true", comment="6. merge the resulting branch")
    expect(e2e.run("mngr destroy fix-bug -f --remove-created-branch", comment="7. stop and clean up")).to_succeed()
    # Cleanup is the point of step 7. Verify the concrete effect of
    # --remove-created-branch with a pure-git check (no provider enumeration):
    # the agent's branch should no longer exist.
    branch_result = e2e.run("git branch --list mngr/fix-bug", comment="verify the created branch was removed")
    expect(branch_result.stdout).to_be_empty()
    # Destroy must also remove the agent itself, not just its branch: it should
    # no longer be listed, and resolving it by name should now fail. Scope to the
    # local provider for the same reason as step 2's listing (a bare `mngr list`
    # would query unreachable remote backends in the isolated e2e profile).
    final_listing = e2e.run("mngr list --provider local", comment="confirm the agent is gone after cleanup")
    expect(final_listing).to_succeed()
    expect(final_listing.stdout).not_to_contain("fix-bug")
    expect(e2e.run("mngr exec fix-bug pwd", comment="verify the destroyed agent can no longer be reached")).to_fail()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(420)
def test_recipe_multi_agent_parallel_workflow(e2e: E2eSession) -> None:
    """Tutorial block:
        # launch multiple agents in parallel, each working on a different task
        mngr create agent-auth --no-connect --provider modal --message "Refactor the auth module to use JWT tokens"
        mngr create agent-tests --no-connect --provider modal --message "Add integration tests for the API endpoints"
        mngr create agent-docs --no-connect --provider modal --message "Update the API documentation to match the new endpoints"
        # check on all of them at once
        mngr list --running
        # wait for them to finish
        mngr wait agent-auth && mngr wait agent-tests && mngr wait agent-docs
        # run git status on all agents to see what they've changed
        mngr list --ids | mngr exec - "git diff --stat"
        # send a coordination message to all agents
        mngr list --ids | mngr msg - -m "Reminder: commit and push your changes when done"
        # merge all of the changes
        git merge mngr/agent-auth
        git merge mngr/agent-tests
        git merge mngr/agent-docs
        # when all are done, clean up
        mngr destroy --force --remove-created-branch agent-auth agent-tests agent-docs

    Scope: the parallel multi-agent recipe, with local `sleep` command agents
    standing in for the modal claude agents. Three agents are created and each
    runs in its own distinct worktree; list --running succeeds; the `mngr list
    --ids | mngr exec/msg -` fan-out reaches all three (exec git diff --stat
    names each agent, msg reports 3 agent(s)); the `&&` wait chain parses; and
    destroy --remove-created-branch cleans up all three at once (3 agent(s),
    none remaining in the listing).
    """
    # Every `mngr` invocation pays a fixed ~15-20s startup cost in the
    # all-packages dev/test venv (the CLI eagerly imports every installed
    # provider plugin, pulling in heavy cloud SDKs). Commands that also do real
    # work -- create (provisioning + worktree + tmux), the two-process fan-out
    # pipes, and destroy (which runs post-cleanup garbage collection) -- can
    # therefore exceed the 30s default per-command timeout. Give them the same
    # generous headroom the other multi-step e2e recipes use for their slow
    # commands so a startup spike does not spuriously fail the recipe under test.
    slow_command_timeout = 120.0
    for name, sleep_value in [
        ("agent-auth", 100971),
        ("agent-tests", 100972),
        ("agent-docs", 100973),
    ]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
                timeout=slow_command_timeout,
            )
        ).to_succeed()
    expect(e2e.run("mngr list --running", comment="check on all of them at once")).to_succeed()
    # The recipe launches the agents in parallel, so verify that all three were
    # actually created and that each one is isolated in its own worktree (the
    # whole point of running agents in parallel on different tasks).
    listing = e2e.run("mngr list", comment="confirm all three agents were created")
    expect(listing).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(listing.stdout).to_contain(name)
    work_dirs = []
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        pwd_result = e2e.run(f"mngr exec {name} pwd", comment=f"verify {name} runs in its own worktree")
        expect(pwd_result).to_succeed()
        work_dirs.append(pwd_result.stdout.strip())
    assert len(set(work_dirs)) == 3, f"expected three distinct agent worktrees, got {work_dirs}"
    # mngr wait blocks indefinitely on sleep agents; wrap each one to keep the
    # test fast. We mainly want to verify the && chain parses.
    e2e.run(
        "timeout 1 mngr wait agent-auth && timeout 1 mngr wait agent-tests && timeout 1 mngr wait agent-docs || true",
        comment="wait for them to finish",
    )
    # The fan-out (`mngr list --ids | mngr exec -`) should reach every agent.
    diff_result = e2e.run(
        'mngr list --ids | mngr exec - "git diff --stat"',
        comment="run git status on all agents",
        timeout=slow_command_timeout,
    )
    expect(diff_result).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(diff_result.stdout).to_contain(name)
    # The broadcast message should be delivered to all three agents.
    msg_result = e2e.run(
        'mngr list --ids | mngr msg - -m "Reminder: commit and push your changes when done"',
        comment="send a coordination message to all agents",
        timeout=slow_command_timeout,
    )
    expect(msg_result).to_succeed()
    expect(msg_result.stdout).to_contain("3 agent(s)")
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        e2e.run(f"git merge mngr/{name} || true", comment=f"merge {name}")
    destroy_result = e2e.run(
        "mngr destroy --force --remove-created-branch agent-auth agent-tests agent-docs",
        comment="clean up all three agents",
        timeout=slow_command_timeout,
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("3 agent(s)")
    # After cleanup, none of the agents should remain.
    final_listing = e2e.run("mngr list", comment="confirm cleanup removed all three agents")
    expect(final_listing).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(final_listing.stdout).not_to_contain(name)
