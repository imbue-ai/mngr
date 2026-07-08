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
@pytest.mark.timeout(60)
def test_exec_branch_show_current(e2e: E2eSession) -> None:
    """Tutorial block:
        # check what branch an agent is on (it may have shifted if the agent checked out a new branch)
        mngr exec my-task "git branch --show-current"

    Scope: `mngr exec` runs `git branch --show-current` inside the agent and
    returns its current branch. By default mngr creates a fresh branch named
    mngr/{agent_name} for the agent, so the output names mngr/my-task rather than
    the original branch.
    """
    _create_my_task(e2e, 100920)
    result = e2e.run(
        'mngr exec my-task "git branch --show-current"',
        comment="check what branch an agent is on",
    )
    expect(result).to_succeed()
    # by default mngr creates a fresh branch named mngr/{agent_name} for the
    # agent, so the agent is sitting on that branch (not the original one).
    expect(result.stdout).to_contain("mngr/my-task")


@pytest.mark.release
def test_list_fields_original_branch(e2e: E2eSession) -> None:
    """Tutorial block:
        # you can see the branch mngr created for each agent as part of the details in "mngr list" as well (field name: "initial_branch")
        mngr list --fields "name,state,initial_branch"

    Scope: the no-agents case of the block. The isolated environment starts with
    no agents, so `mngr list --fields "name,state,initial_branch"` must still
    parse the --fields flag (including the initial_branch field) and exit cleanly,
    reporting "No agents found".
    """
    result = e2e.run(
        'mngr list --fields "name,state,initial_branch"',
        comment="list with initial_branch field",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_fields_original_branch_with_agent(e2e: E2eSession) -> None:
    """Tutorial block:
        # you can see the branch mngr created for each agent as part of the details in "mngr list" as well (field name: "initial_branch")
        mngr list --fields "name,state,initial_branch"

    Scope: the happy path of the block (counterpart to
    test_list_fields_original_branch). With an agent present, the initial_branch
    column actually displays the branch mngr created for it (mngr/{agent_name} by
    default), so the row shows both the agent name (my-task) and mngr/my-task.
    """
    _create_my_task(e2e, 100921)
    result = e2e.run(
        'mngr list --fields "name,state,initial_branch"',
        comment="list with initial_branch field",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # The agent row must appear with both its name and the branch mngr created.
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_git_status_short(e2e: E2eSession) -> None:
    """Tutorial block:
        # check if the agent has uncommitted changes
        mngr exec my-task "git status --short"

    Scope: `mngr exec` runs `git status --short` inside the agent. An untracked
    file deterministically created in the agent's workspace shows up in the
    porcelain output, confirming the command actually reports uncommitted changes
    rather than merely exiting 0.
    """
    _create_my_task(e2e, 100921)
    # Deterministically introduce an uncommitted change in the agent's
    # workspace so the tutorial command has something concrete to report,
    # rather than relying on incidental untracked files.
    expect(
        e2e.run('mngr exec my-task "touch uncommitted_change.txt"', comment="create an uncommitted change")
    ).to_succeed()
    result = e2e.run('mngr exec my-task "git status --short"', comment="check uncommitted changes")
    expect(result).to_succeed()
    # `git status --short` prints one porcelain line per changed path; the
    # untracked file we just created must show up as `?? uncommitted_change.txt`,
    # confirming the command actually reports uncommitted changes (not just exits 0).
    assert "uncommitted_change.txt" in result.stdout, (
        f"expected the untracked file in porcelain output, got: {result.stdout!r}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_exec_git_log(e2e: E2eSession) -> None:
    """Tutorial block:
        # see the agent's recent commits
        mngr exec my-task "git log --oneline -5"

    Scope: `mngr exec` runs `git log --oneline -5` against the agent's checkout.
    The agent inherits the test repo's history, so its log shows the base
    "Initial commit" in the "<short-hash> <subject>" --oneline shape, proving the
    log actually ran against the agent's checkout rather than just exiting 0.
    """
    _create_my_task(e2e, 100922)
    result = e2e.run('mngr exec my-task "git log --oneline -5"', comment="see recent commits")
    expect(result).to_succeed()
    # The agent inherits the test repo's history, so its log must show the
    # base commit -- this proves git log actually ran against the agent's
    # checkout rather than just exiting 0.
    expect(result.stdout).to_contain("Initial commit")
    # --oneline output is "<short-hash> <subject>"; assert that shape so a
    # future regression to a different log format would be caught.
    expect(result.stdout).to_match(r"(?m)^\s*[0-9a-f]{7,40} \S")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_message_commit_request(e2e: E2eSession) -> None:
    """Tutorial block:
        # ask the agent to commit its work
        mngr msg my-task -m "Please commit all your changes with a descriptive message"

    Scope: `mngr msg` routes and delivers the message to the named agent (not
    merely parsing the command). The human output names the target agent
    ("Message sent to: my-task") and reports a successful send count of one.
    """
    _create_my_task(e2e, 100923)
    msg_result = e2e.run(
        'mngr msg my-task -m "Please commit all your changes with a descriptive message"',
        comment="ask the agent to commit",
    )
    expect(msg_result).to_succeed()
    # Verify the message was actually routed to and delivered to the agent, not
    # merely that the command parsed: the human output names the target agent
    # and reports a successful send count of one.
    expect(msg_result.stdout).to_contain("Message sent to: my-task")
    expect(msg_result.stdout).to_contain("Successfully sent message to 1 agent(s)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_force_commit(e2e: E2eSession) -> None:
    """Tutorial block:
        # or forcibly commit all of it yourself
        mngr exec my-task 'git add . && git commit -m "WIP: save agent progress"'

    Scope: `mngr exec` runs `git add . && git commit` inside the agent, forcibly
    committing its uncommitted changes. Afterward the "WIP: save agent progress"
    message is the agent's most recent commit and the previously-uncommitted file
    is no longer reported by `git status --short`.
    """
    _create_my_task(e2e, 100924)
    # Create an uncommitted change in the agent so the force-commit has
    # something concrete to capture.
    expect(
        e2e.run(
            'mngr exec my-task "echo scratch > wip_file.txt"',
            comment="create an uncommitted change in the agent",
        )
    ).to_succeed()
    # forcibly commit all of it
    expect(
        e2e.run(
            "mngr exec my-task 'git add . && git commit -m \"WIP: save agent progress\"'",
            comment="forcibly commit all of it",
        )
    ).to_succeed()
    # The commit message should now be the agent's most recent commit, proving
    # the force-commit actually landed.
    log_result = e2e.run(
        'mngr exec my-task "git log --oneline -1"',
        comment="verify the commit landed",
    )
    expect(log_result).to_succeed()
    assert "WIP: save agent progress" in log_result.stdout, log_result.stdout
    # ...and the previously-uncommitted file is no longer reported as a change.
    status_result = e2e.run(
        'mngr exec my-task "git status --short"',
        comment="verify the change was committed",
    )
    expect(status_result).to_succeed()
    assert "wip_file.txt" not in status_result.stdout, status_result.stdout


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_all_git_status(e2e: E2eSession) -> None:
    """Tutorial block:
        # check all agents' git status at once
        mngr list --ids | mngr exec - "git status --short"

    Scope: piping `mngr list --ids` into `mngr exec -` fans the `git status
    --short` command out across all agents. The fan-out actually reaches the
    agent (rather than merely exiting 0): mngr reports the per-agent outcome, so
    my-task appears in the output.
    """
    _create_my_task(e2e, 100925)
    # `mngr list` attempts remote (Modal) discovery in addition to the local
    # agent, so the piped command can exceed the default run_command timeout;
    # give it ample headroom.
    result = e2e.run(
        'mngr list --ids | mngr exec - "git status --short"',
        comment="check all agents' git status at once",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # Verify the fan-out actually reached the agent rather than merely exiting
    # 0: mngr reports the per-agent outcome (e.g. "Command succeeded on agent
    # my-task") in the output.
    assert "my-task" in result.stdout, f"expected my-task in fan-out output, got: {result.stdout!r}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_git_merge_agent_branch(e2e: E2eSession) -> None:
    """Tutorial block:
        # merge the agent's work like normal if the agent is local:
        git merge mngr/my-task

    Scope: for a local agent, a plain `git merge mngr/my-task` integrates the
    agent's branch into the caller's tree. After the agent commits a new file on
    its own branch, the merge brings that committed file into the caller's working
    tree (so it is not a no-op fast-forward of identical content).
    """
    _create_my_task(e2e, 100926)
    # The agent's branch is created at the caller's HEAD, so it must exist.
    expect(e2e.run("git rev-parse --verify mngr/my-task", comment="confirm the agent's branch exists")).to_succeed()
    # Give the branch real work to integrate: have the agent commit a new file
    # on its own branch, so the merge below is not a no-op fast-forward.
    expect(
        e2e.run(
            "mngr exec my-task 'echo agent-change > agent_work.txt && "
            'git add agent_work.txt && git commit -m "agent work"\'',
            comment="have the agent commit work on its branch",
        )
    ).to_succeed()
    # merge the agent's work like normal if the agent is local
    expect(e2e.run("git merge mngr/my-task", comment="merge the agent's work")).to_succeed()
    # The merge must bring the agent's committed file into the caller's tree.
    merged = e2e.run("cat agent_work.txt", comment="verify the agent's work is now present")
    expect(merged).to_succeed()
    expect(merged.stdout).to_contain("agent-change")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_git_push_then_merge(e2e: E2eSession) -> None:
    """Tutorial block:
        # and if remote, force the agent to push, then fetch and merge:
        mngr exec my-task "git push origin mngr/my-task"
        git fetch --all && git merge mngr/my-task
        # in general, you should probably just tell your agents to automatically push / create PRs when it makes sense

    Scope: covers both halves of the remote merge recipe. The forced push runs on
    the agent host via `mngr exec`; since temp_git_repo has no `origin` remote it
    fails, and the agent-side git error (mentioning "origin" and naming my-task)
    coming back proves mngr forwarded the quoted command to the agent and surfaced
    its non-zero exit. The caller-side `git fetch --all && git merge` then runs
    locally: with no remotes the fetch is a no-op and the agent's branch (same
    content as the current branch) merges as "Already up to date".
    """
    _create_my_task(e2e, 100927)

    # `temp_git_repo` has no `origin` remote, so the agent's push fails. The
    # specific git error ("'origin' does not appear to be a git repository")
    # coming back proves that mngr forwarded the quoted command to the agent
    # host, ran it there, and surfaced the agent-side non-zero exit code.
    push_result = e2e.run(
        'mngr exec my-task "git push origin mngr/my-task"',
        comment="force the agent to push",
    )
    expect(push_result).to_fail()
    expect(push_result.stderr).to_contain("origin")
    expect(push_result.stderr).to_contain("my-task")

    # The caller-side fetch + merge still runs locally. With no remotes,
    # `git fetch --all` is a no-op, and the agent's branch (created by `mngr
    # create`, same content as the current branch) merges as an up-to-date
    # no-op rather than failing.
    merge_result = e2e.run("git fetch --all && git merge mngr/my-task", comment="fetch and merge locally")
    expect(merge_result).to_succeed()
    expect(merge_result.stdout).to_contain("Already up to date")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_destroy_remove_created_branch_inline(e2e: E2eSession) -> None:
    """Tutorial block:
        # when destroying, clean up the branch that was originally created when the agent was created
        mngr destroy my-task --force --remove-created-branch

    Scope: `mngr destroy --force --remove-created-branch` destroys the agent and
    deletes the branch mngr created for it. The output reports "Deleted branch:
    mngr/my-task" and, as a verified before/after, the branch that existed prior
    to destroy is actually gone from the repo afterward.
    """
    _create_my_task(e2e, 100928)
    # Creating the agent makes a dedicated branch (mngr/my-task) in the repo;
    # confirm it exists so the post-destroy check is a meaningful before/after.
    branch_before = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's created branch exists before destroy",
    )
    expect(branch_before.stdout).to_contain("mngr/my-task")
    destroy_result = e2e.run(
        "mngr destroy my-task --force --remove-created-branch",
        comment="destroy and remove created branch",
    )
    expect(destroy_result).to_succeed()
    # --remove-created-branch reports deleting the branch it created for the agent.
    expect(destroy_result.stdout).to_contain("Deleted branch: mngr/my-task")
    # Verify the concrete effect: the branch is actually gone from the repo.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's created branch was removed",
    )
    expect(branch_after.stdout).to_be_empty()
