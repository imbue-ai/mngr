"""Tests for the WORKING WITH GIT tutorial section."""

import shlex
import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Modal create (provisioning the sandbox + deploying the snapshot function)
# is far slower than the default Session.run timeout, so each remote command
# needs a generous explicit timeout. Matches test_create_modal._REMOTE_TIMEOUT.
_REMOTE_TIMEOUT = 120.0


def _worktree_path_for_branch(porcelain_output: str, branch: str) -> str | None:
    """Return the worktree path checked out on `branch`, parsing `git worktree list --porcelain`.

    Each porcelain record starts with a `worktree <path>` line and (for a
    non-detached worktree) a `branch refs/heads/<name>` line. Returns None when
    no worktree is checked out on the requested branch.
    """
    current_path: str | None = None
    for line in porcelain_output.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}":
            return current_path
    return None


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    # Create the agent on Modal so the test exercises the real remote path
    # (Modal CLI + rsync sync), matching the @pytest.mark.modal /
    # @pytest.mark.rsync marks. Note that tmux runs on the remote host for a
    # Modal agent, so the local tmux binary is not invoked (no @pytest.mark.tmux).
    expect(
        e2e.run(
            f"mngr create my-task --provider modal --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
            timeout=_REMOTE_TIMEOUT,
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_branch_show_current(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check what branch an agent is on (it may have shifted if the agent checked out a new branch)
        mngr exec my-task "git branch --show-current"
    """)
    _create_my_task(e2e, 100920)
    result = e2e.run(
        'mngr exec my-task "git branch --show-current"',
        comment="check what branch an agent is on",
    )
    expect(result).to_succeed()
    # By default mngr creates a branch named mngr/{agent_name} for each agent,
    # so the agent should report that it is sitting on mngr/my-task.
    expect(result.stdout).to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_branch_show_current_after_checkout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check what branch an agent is on (it may have shifted if the agent checked out a new branch)
        mngr exec my-task "git branch --show-current"
    """)
    _create_my_task(e2e, 100929)
    # The tutorial comment notes the branch "may have shifted if the agent
    # checked out a new branch"; simulate that by checking out a new branch in
    # the agent's worktree, then verify git branch --show-current reflects it.
    expect(
        e2e.run(
            'mngr exec my-task "git checkout -b shifted-branch"',
            comment="agent checks out a new branch",
        )
    ).to_succeed()
    result = e2e.run(
        'mngr exec my-task "git branch --show-current"',
        comment="check what branch an agent is on after it shifted",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("shifted-branch")
    # The agent has moved off the auto-created branch, so it is no longer
    # reported as the current branch.
    expect(result.stdout).not_to_contain("mngr/my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_list_fields_original_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can see the branch created for each agent as part of the details in "mngr list" as well (field name: "initial_branch")
        mngr list --fields "name,state,initial_branch"
    """)
    # By default mngr creates the branch mngr/{agent_name} for each agent, so the
    # initial_branch field should report "mngr/my-task" for the agent created here.
    _create_my_task(e2e, 100929)
    result = e2e.run(
        'mngr list --fields "name,state,initial_branch"',
        comment="you can see the branch created for each agent as part of the details in mngr list",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("mngr/my-task")

    # Cross-check the field against the structured JSON output to ensure the
    # human table value is the real initial_branch field and not a coincidence.
    json_result = e2e.run("mngr list --format json", comment="verify initial_branch via JSON output")
    expect(json_result).to_succeed()
    parsed = json.loads(json_result.stdout)
    matching_agents = [agent for agent in parsed["agents"] if agent["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["initial_branch"] == "mngr/my-task"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_git_status_short(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check if the agent has uncommitted changes
        mngr exec my-task "git status --short"
    """)
    _create_my_task(e2e, 100921)
    # Create a known uncommitted change in the agent's git checkout (exec runs in
    # the agent's work_dir by default), then verify that `git status --short`
    # reports it with the porcelain "??" untracked marker.
    expect(
        e2e.run('mngr exec my-task "touch uncommitted_marker.txt"', comment="create an uncommitted change")
    ).to_succeed()
    result = e2e.run('mngr exec my-task "git status --short"', comment="check uncommitted changes")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("?? uncommitted_marker.txt")


@pytest.mark.timeout(300)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
def test_exec_git_log(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see the agent's recent commits
        mngr exec my-task "git log --oneline -5"
    """)
    _create_my_task(e2e, 100922)
    result = e2e.run(
        'mngr exec my-task "git log --oneline -5"',
        comment="see recent commits",
        timeout=_REMOTE_TIMEOUT,
    )
    expect(result).to_succeed()
    # The agent's repo is created from the test's temp git repo, so its log must
    # contain the seed commit created by the fixture. Assert on the actual
    # output rather than only the exit code.
    assert "Initial commit" in result.stdout, f"Expected seed commit in git log output, got:\n{result.stdout}"


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
@pytest.mark.timeout(180)
def test_exec_force_commit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or forcibly commit all of it yourself
        mngr exec my-task 'git add . && git commit -m "WIP: save agent progress"'
    """)
    _create_my_task(e2e, 100924)
    # The agent's synced repo contains the untracked project config dir
    # (.mngr-test-*/settings.local.toml), so `git add . && git commit` always
    # has something to stage and the forced commit succeeds.
    expect(
        e2e.run(
            "mngr exec my-task 'git add . && git commit -m \"WIP: save agent progress\"'",
            comment="forcibly commit all of it",
        )
    ).to_succeed()
    # Verify the commit actually landed: the agent's HEAD subject must be the
    # message we forced. (mngr appends its own "Command succeeded" footer to the
    # exec output, so we assert on a substring rather than exact equality.)
    head_subject = e2e.run('mngr exec my-task "git log -1 --pretty=%s"', comment="verify the forced commit landed")
    expect(head_subject).to_succeed()
    expect(head_subject.stdout).to_contain("WIP: save agent progress")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_exec_all_git_status(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check all agents' git status at once
        mngr list --ids | mngr exec - "git status --short"
    """)
    _create_my_task(e2e, 100925)
    result = e2e.run(
        'mngr list --ids | mngr exec - "git status --short"',
        comment="check all agents' git status at once",
    )
    expect(result).to_succeed()
    # Verify the fan-out actually happened: `mngr list --ids` must have piped the
    # agent id into `mngr exec -`, which then ran `git status --short` on the one
    # agent we created. The per-agent success line proves the command reached it,
    # rather than the pipeline merely exiting 0 with no agents targeted.
    expect(result.stdout).to_contain("Command succeeded on agent my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_git_merge_agent_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # merge the agent's work like normal if the agent is local:
        git merge mngr/my-task
    """)
    _create_my_task(e2e, 100926)
    # The agent runs in a local git worktree checked out on branch mngr/my-task.
    # Locate that worktree and commit a file there, so the merge below brings in
    # real work rather than being a no-op.
    worktrees = e2e.run("git worktree list --porcelain", comment="locate the agent's worktree")
    expect(worktrees).to_succeed()
    agent_worktree = _worktree_path_for_branch(worktrees.stdout, "mngr/my-task")
    assert agent_worktree is not None, f"no worktree checked out on mngr/my-task:\n{worktrees.stdout}"
    quoted_worktree = shlex.quote(agent_worktree)
    expect(
        e2e.run(
            f"sh -c 'echo merged-by-agent > {quoted_worktree}/agent_work.txt"
            f" && git -C {quoted_worktree} add agent_work.txt"
            f' && git -C {quoted_worktree} commit -m "agent work"\'',
            comment="have the agent's branch make a commit",
        )
    ).to_succeed()
    # merge the agent's work like normal if the agent is local:
    expect(e2e.run("git merge mngr/my-task", comment="merge the agent's work")).to_succeed()
    # the agent's commit now lives on the current branch: its file is present
    # in the working tree and its commit is in the branch history.
    merged_file = e2e.run("cat agent_work.txt", comment="verify the agent's file was merged in")
    expect(merged_file).to_succeed()
    expect(merged_file.stdout).to_contain("merged-by-agent")
    expect(e2e.run("git log --oneline -3", comment="confirm the agent's commit is in history").stdout).to_contain(
        "agent work"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_git_push_then_merge(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # and if remote, force the agent to push, then fetch and merge:
        mngr exec my-task "git push origin mngr/my-task"
        git fetch --all && git merge mngr/my-task
        # in general, you should probably just tell your agents to automatically push / create PRs when it makes sense
    """)
    _create_my_task(e2e, 100927)
    # No remote `origin` is configured in the test repo, so the push fails.
    # Verify that `mngr exec` actually forwards the quoted git command to the
    # agent and surfaces the agent-side failure (a missing `origin` remote)
    # rather than a mngr-side error -- this proves the command ran on the agent.
    push_result = e2e.run(
        'mngr exec my-task "git push origin mngr/my-task"',
        comment="force the agent to push",
    )
    expect(push_result).to_fail()
    expect(push_result.stderr).to_contain("origin")

    # The caller-side fetch + merge chain runs cleanly: there are no git remotes
    # to fetch from, and `mngr/my-task` was branched from the current commit, so
    # the merge is a fast-forward no-op.
    merge_result = e2e.run("git fetch --all && git merge mngr/my-task", comment="fetch and merge locally")
    expect(merge_result).to_succeed()

    # Confirm the agent's branch was actually integrated: after the merge it must
    # be an ancestor of HEAD (here, equal to it).
    expect(
        e2e.run(
            "git merge-base --is-ancestor mngr/my-task HEAD",
            comment="verify the agent branch is merged into HEAD",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_remove_created_branch_inline(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # when destroying, clean up the branch that was originally created when the agent was created
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100928)

    # Creating the agent should have created the mngr/my-task branch in the repo.
    expect(e2e.run("git branch --list mngr/my-task", comment="confirm the agent branch exists")).to_succeed()
    expect(e2e.run("git branch --list mngr/my-task").stdout).to_contain("mngr/my-task")

    destroy_result = e2e.run(
        "mngr destroy my-task --force --remove-created-branch",
        comment="destroy and remove created branch",
    )
    expect(destroy_result).to_succeed()
    # The command should report both the agent destruction and the branch removal.
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    expect(destroy_result.stdout).to_contain("Deleted branch: mngr/my-task")

    # The agent is gone and the branch it created has actually been removed.
    expect(e2e.run("mngr list", comment="verify the agent no longer exists").stdout).not_to_contain("my-task")
    expect(
        e2e.run("git branch --list mngr/my-task", comment="verify the created branch was removed").stdout
    ).to_be_empty()
