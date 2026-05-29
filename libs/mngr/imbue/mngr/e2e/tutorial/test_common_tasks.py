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
@pytest.mark.modal
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
    expect(e2e.run("mngr transcript fix-bug --tail 3", comment="3. check transcript")).to_succeed()
    expect(e2e.run('mngr msg fix-bug -m "lint please"', comment="4. send a follow-up message")).to_succeed()
    expect(e2e.run("mngr conn fix-bug", comment="5. connect to review")).to_succeed()
    e2e.run("git merge mngr/fix-bug || true", comment="6. merge the resulting branch")
    expect(e2e.run("mngr destroy fix-bug -f --remove-created-branch", comment="7. stop and clean up")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
        mngr msg -a -m "Reminder: commit and push your changes when done"
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
    expect(
        e2e.run(
            'mngr list --ids | mngr exec - "git diff --stat"',
            comment="run git status on all agents",
        )
    ).to_succeed()
    expect(
        e2e.run(
            'mngr msg -a -m "Reminder: commit and push your changes when done"',
            comment="send a coordination message to all agents",
        )
    ).to_succeed()
    for name in ("agent-auth", "agent-tests", "agent-docs"):
        e2e.run(f"git merge mngr/{name} || true", comment=f"merge {name}")
    expect(
        e2e.run(
            "mngr destroy --force --remove-created-branch agent-auth agent-tests agent-docs",
            comment="clean up all three agents",
        )
    ).to_succeed()
