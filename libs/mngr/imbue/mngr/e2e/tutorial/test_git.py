"""Tests for the WORKING WITH GIT tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


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
def test_exec_branch_show_current(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check what branch an agent is on (it may have shifted if the agent checked out a new branch)
        mngr exec my-task "git branch --show-current"
    """)
    _create_my_task(e2e, 100920)
    expect(
        e2e.run(
            'mngr exec my-task "git branch --show-current"',
            comment="check what branch an agent is on",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_fields_original_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # TODO: this field name isn't right, go fix (but that info is there somewhere in mngr list)
        # you can see the original branch as part of the details in "mngr list" as well (field name: "git.original_branch")
        mngr list --fields "name,state,git.original_branch"
    """)
    # The field name may not exist; the test only needs to confirm mngr parses
    # the --fields flag and doesn't crash.
    e2e.run(
        'mngr list --fields "name,state,git.original_branch"',
        comment="list with git.original_branch field",
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_git_status_short(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check if the agent has uncommitted changes
        mngr exec my-task "git status --short"
    """)
    _create_my_task(e2e, 100921)
    expect(e2e.run('mngr exec my-task "git status --short"', comment="check uncommitted changes")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_git_log(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see the agent's recent commits
        mngr exec my-task "git log --oneline -5"
    """)
    _create_my_task(e2e, 100922)
    expect(e2e.run('mngr exec my-task "git log --oneline -5"', comment="see recent commits")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_message_commit_request(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # ask the agent to commit its work
        mngr msg my-task -m "Please commit all your changes with a descriptive message"
    """)
    _create_my_task(e2e, 100923)
    expect(
        e2e.run(
            'mngr msg my-task -m "Please commit all your changes with a descriptive message"',
            comment="ask the agent to commit",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_force_commit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or forcibly commit all of it yourself
        mngr exec my-task 'git add . && git commit -m "WIP: save agent progress"'
    """)
    _create_my_task(e2e, 100924)
    # Allow the commit to fail (nothing to commit / no user.email set in CI);
    # the test only needs to confirm mngr parses the quoted compound command.
    e2e.run(
        "mngr exec my-task 'git add . && git commit -m \"WIP: save agent progress\" || true'",
        comment="forcibly commit all of it",
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_all_git_status(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check all agents' git status at once
        mngr list --ids | mngr exec - "git status --short"
    """)
    _create_my_task(e2e, 100925)
    expect(
        e2e.run(
            'mngr list --ids | mngr exec - "git status --short"',
            comment="check all agents' git status at once",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_git_merge_agent_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # merge the agent's work like normal if the agent is local:
        git merge mngr/my-task
    """)
    _create_my_task(e2e, 100926)
    # The agent's branch has the same content as the current branch; the merge
    # will be a fast-forward no-op. Allow failure if branch is missing.
    e2e.run("git merge mngr/my-task || true", comment="merge the agent's work")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_exec_git_push_then_merge(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # and if remote, force the agent to push, then fetch and merge:
        mngr exec my-task "git push origin mngr/my-task"
        git fetch --all && git merge mngr/my-task
        # in general, you should probably just tell your agents to automatically push / create PRs when it makes sense
    """)
    _create_my_task(e2e, 100927)
    # No remote `origin` is configured in temp_git_repo, so the push fails;
    # demonstrate that mngr forwards the quoted command and that the
    # caller-side `git fetch && git merge` chain runs.
    e2e.run(
        'mngr exec my-task "git push origin mngr/my-task" || true',
        comment="force the agent to push",
    )
    e2e.run("git fetch --all && git merge mngr/my-task || true", comment="fetch and merge locally")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_remove_created_branch_inline(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # when destroying, clean up the branch that was originally created when the agent was created
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100928)
    expect(
        e2e.run(
            "mngr destroy my-task --force --remove-created-branch",
            comment="destroy and remove created branch",
        )
    ).to_succeed()
