"""Tests for the COMMON TASKS recipe block and the MULTI-AGENT WORKFLOWS recipe.

These two blocks are multi-step recipes spanning several commands. Each test
mirrors the recipe shape but substitutes lightweight equivalents (sleep agents
in place of modal claude agents) so the test stays fast.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_recipe_launch_check_cleanup(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
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
    """)
    # Substitute a local command agent for the modal claude agent; the recipe
    # shape (create -> list -> transcript -> msg -> conn -> merge -> destroy)
    # is what we want to verify.
    expect(
        e2e.run(
            "mngr create fix-bug --type command --no-ensure-clean --no-connect -- sleep 100970",
            comment="1. create an agent for the task",
        )
    ).to_succeed()
    expect(e2e.run("mngr list --running", comment="2. check what agents are running")).to_succeed()
    # `mngr list --running` filters to agents in the RUNNING state. The recipe's
    # working claude agent would be RUNNING, but our backgrounded `sleep` command
    # agent sits in WAITING, so it does not appear there. Confirm the agent was
    # actually created and is tracked via an unfiltered listing.
    list_result = e2e.run("mngr list", comment="verify the agent was created and is tracked")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("fix-bug")
    # The recipe targets a modal claude agent, which produces a common transcript.
    # Our command-agent substitute does not implement the transcript mixin, so the
    # command reports a clear, type-specific error instead of failing opaquely.
    transcript_result = e2e.run("mngr transcript fix-bug --tail 3", comment="3. check transcript")
    expect(transcript_result).to_fail()
    expect(transcript_result.stderr).to_contain("does not produce a common transcript")
    expect(e2e.run('mngr msg fix-bug -m "lint please"', comment="4. send a follow-up message")).to_succeed()
    # `mngr conn` ends in an interactive tmux attach that requires a real
    # controlling terminal. The piped test runner has none, so the attach itself
    # cannot complete here. We still run the real command and assert it drove the
    # full connect pipeline -- resolving the agent and reaching the tmux attach --
    # which surfaces as tmux's "not a terminal" error in this headless context.
    # A human running this step in a terminal attaches successfully.
    connect_result = e2e.run("mngr conn fix-bug", comment="5. connect to review")
    expect(connect_result).to_fail()
    expect(connect_result.stderr).to_contain("Connecting to agent: fix-bug")
    expect(connect_result.stderr).to_contain("not a terminal")
    # The agent's branch is created at agent-creation time, so it exists even
    # though the sleep agent never committed anything (the merge is a no-op here).
    expect(e2e.run("git rev-parse --verify mngr/fix-bug", comment="the agent branch exists")).to_succeed()
    e2e.run("git merge mngr/fix-bug || true", comment="6. merge the resulting branch")
    expect(e2e.run("mngr destroy fix-bug -f --remove-created-branch", comment="7. stop and clean up")).to_succeed()
    # Verify the concrete effects of destroy: the agent is gone from the listing
    # and --remove-created-branch deleted the mngr/fix-bug branch.
    after_destroy = e2e.run("mngr list", comment="verify the agent was destroyed")
    expect(after_destroy).to_succeed()
    expect(after_destroy.stdout).not_to_contain("fix-bug")
    expect(e2e.run("git rev-parse --verify mngr/fix-bug", comment="the agent branch was removed")).to_fail()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_recipe_multi_agent_parallel_workflow(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
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
    """)
    for name, sleep_value in [
        ("agent-auth", 100971),
        ("agent-tests", 100972),
        ("agent-docs", 100973),
    ]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()
    expect(e2e.run("mngr list --running", comment="check on all of them at once")).to_succeed()
    # mngr wait blocks indefinitely on sleep agents; wrap each one to keep the
    # test fast. We mainly want to verify the && chain parses.
    e2e.run(
        "timeout 1 mngr wait agent-auth && timeout 1 mngr wait agent-tests && timeout 1 mngr wait agent-docs || true",
        comment="wait for them to finish",
    )
    # The exec fans out to every agent (resolved via `mngr list --ids`), so a
    # successful run must reach all three. Asserting each name appears confirms
    # the pipe really targeted all of them rather than silently hitting a subset.
    exec_result = e2e.run(
        'mngr list --ids | mngr exec - "git diff --stat"',
        comment="run git status on all agents",
    )
    expect(exec_result).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(exec_result.stdout).to_contain(name)
    # The coordination message likewise fans out to all three agents.
    msg_result = e2e.run(
        'mngr list --ids | mngr msg - -m "Reminder: commit and push your changes when done"',
        comment="send a coordination message to all agents",
    )
    expect(msg_result).to_succeed()
    expect(msg_result.stdout).to_contain("Successfully sent message to 3 agent(s)")
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        e2e.run(f"git merge mngr/{name} || true", comment=f"merge {name}")
    destroy_result = e2e.run(
        "mngr destroy --force --remove-created-branch agent-auth agent-tests agent-docs",
        comment="clean up all three agents",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Successfully destroyed 3 agent(s)")
    # --remove-created-branch must delete each agent's branch.
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(destroy_result.stdout).to_contain(f"Deleted branch: mngr/{name}")
    # Verify the concrete cleanup effect: the agents are gone from the listing
    # and their branches no longer exist in the repo.
    final_list = e2e.run("mngr list", comment="verify all agents were destroyed")
    expect(final_list).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        expect(final_list.stdout).not_to_contain(name)
    branches_result = e2e.run("git branch --list 'mngr/agent-*'", comment="verify agent branches were removed")
    expect(branches_result).to_succeed()
    expect(branches_result.stdout).to_be_empty()
