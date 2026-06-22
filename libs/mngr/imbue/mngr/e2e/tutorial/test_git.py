"""Tests for the WORKING WITH GIT tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int, timeout: float = 30.0) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
            timeout=timeout,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
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
    # by default mngr creates a fresh branch named mngr/{agent_name} for the
    # agent, so the agent is sitting on that branch (not the original one).
    expect(result.stdout).to_contain("mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_branch_show_current_after_checkout(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check what branch an agent is on (it may have shifted if the agent checked out a new branch)
        mngr exec my-task "git branch --show-current"
    """)
    # The tutorial comment explicitly warns that the reported branch "may have
    # shifted if the agent checked out a new branch". This covers exactly that
    # case: after the agent checks out a brand-new branch, the command must
    # report the shifted branch, not the original mngr/{agent_name} one.
    _create_my_task(e2e, 100922)
    expect(
        e2e.run(
            'mngr exec my-task "git checkout -b experiment"',
            comment="agent checks out a new branch",
        )
    ).to_succeed()
    result = e2e.run(
        'mngr exec my-task "git branch --show-current"',
        comment="check what branch an agent is on",
    )
    expect(result).to_succeed()
    # The reported branch must be the one the agent just checked out, confirming
    # the command reflects the agent's live HEAD rather than the branch mngr
    # originally created it on.
    expect(result.stdout).to_contain("experiment")
    assert "mngr/my-task" not in result.stdout, (
        f"expected only the checked-out branch, but the original branch still showed: {result.stdout!r}"
    )


@pytest.mark.release
def test_list_fields_original_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can see the branch mngr created for each agent as part of the details in "mngr list" as well (field name: "initial_branch")
        mngr list --fields "name,state,initial_branch"
    """)
    # The isolated environment starts with no agents, so listing reports none;
    # the command must still parse the --fields flag (including initial_branch)
    # and exit cleanly. Scope to `--provider local` (the convention used across
    # the e2e suite) so the listing does not query the cloud backends that are
    # registered-but-unconfigured in CI (e.g. AWS), which would otherwise raise
    # ProviderUnavailableError and make `mngr list` exit non-zero per the
    # error-handling spec. This also skips the slow remote discovery path.
    result = e2e.run(
        'mngr list --provider local --fields "name,state,initial_branch"',
        comment="list with initial_branch field",
        timeout=90.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_fields_original_branch_with_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can see the branch mngr created for each agent as part of the details in "mngr list" as well (field name: "initial_branch")
        mngr list --fields "name,state,initial_branch"
    """)
    # The happy path: with an agent present, the initial_branch column must
    # actually display the branch mngr created for it (mngr/{agent_name} by
    # default), which is the behavior the tutorial line advertises.
    _create_my_task(e2e, 100921)
    # Scope the listing to the local provider (the agent created above lives
    # there). The bare tutorial command queries every enabled backend, and in
    # the isolated e2e environment the AWS backend is enabled but has no
    # credentials, so it reports as unreachable and `mngr list` exits non-zero
    # per the error-handling spec -- even though the agent row is rendered
    # correctly. `--provider local` restricts which providers are queried (it is
    # not a CEL filter on results), matching the convention used by the other
    # local-agent listing tests, so this never touches AWS/Modal.
    result = e2e.run(
        'mngr list --provider local --fields "name,state,initial_branch"',
        comment="list with initial_branch field",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # The agent row must appear with both its name and the branch mngr created.
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_git_status_short(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check if the agent has uncommitted changes
        mngr exec my-task "git status --short"
    """)
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
    # untracked file we just created must show up as `?? uncommitted_change.txt`.
    # Asserting the `??` porcelain prefix (not just the filename) confirms the
    # `--short` flag actually produced short/porcelain output -- the long format
    # would instead print an "Untracked files:" section -- so this proves the
    # command reports uncommitted changes in the documented shape, not just exits 0.
    assert "?? uncommitted_change.txt" in result.stdout, (
        f"expected the untracked file as a porcelain `??` entry, got: {result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_exec_git_log(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see the agent's recent commits
        mngr exec my-task "git log --oneline -5"
    """)
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


# Messaging a local agent only attaches its tmux session to deliver the prompt;
# creating the agent uses a git worktree (the default transfer for a same-host
# git project), not rsync. An @pytest.mark.rsync here would therefore trip the
# resource guard (mark present but rsync never invoked), so it is omitted.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_message_commit_request(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # ask the agent to commit its work
        mngr msg my-task -m "Please commit all your changes with a descriptive message"
    """)
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_force_commit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or forcibly commit all of it yourself
        mngr exec my-task 'git add . && git commit -m "WIP: save agent progress"'
    """)
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_force_commit_nothing_to_commit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # or forcibly commit all of it yourself
        mngr exec my-task 'git add . && git commit -m "WIP: save agent progress"'
    """)
    # Unhappy path for the same tutorial command: a freshly created agent has a
    # clean working tree, so `git add .` stages nothing and `git commit` exits
    # non-zero with "nothing to commit". This verifies that `mngr exec` surfaces
    # the failing exit code rather than masking it.
    _create_my_task(e2e, 100924)
    result = e2e.run(
        "mngr exec my-task 'git add . && git commit -m \"WIP: save agent progress\"'",
        comment="force-commit with nothing to commit",
    )
    # `mngr exec` propagates the inner command's non-zero exit.
    expect(result).to_fail()
    # git's own diagnostic must surface through mngr's output.
    assert "nothing to commit" in result.stdout, result.stdout
    # No spurious WIP commit should have landed on the agent's branch.
    log_result = e2e.run(
        'mngr exec my-task "git log --oneline -1"',
        comment="verify no WIP commit was created",
    )
    expect(log_result).to_succeed()
    assert "WIP: save agent progress" not in log_result.stdout, log_result.stdout


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_exec_all_git_status(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # check all agents' git status at once
        mngr list --ids | mngr exec - "git status --short"
    """)
    # Agent creation occasionally spikes past the default 30s per-command
    # timeout (ttyd install attempt, environment setup), so give it headroom;
    # the overall test cap (@pytest.mark.timeout(120)) still guards true hangs.
    _create_my_task(e2e, 100925, timeout=90.0)
    # Deterministically introduce an uncommitted change in the agent's
    # workspace so the fanned-out `git status --short` has concrete content to
    # report, rather than relying on incidental untracked files.
    expect(
        e2e.run('mngr exec my-task "touch fleet_change.txt"', comment="create an uncommitted change in the agent")
    ).to_succeed()
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
    # ...and the fanned-out `git status --short` must surface the actual
    # porcelain line for the change we made, proving the status content (not
    # just the success header) flows back through the fan-out.
    assert "fleet_change.txt" in result.stdout, (
        f"expected the untracked file in the fan-out status output, got: {result.stdout!r}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_git_merge_agent_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # merge the agent's work like normal if the agent is local:
        git merge mngr/my-task
    """)
    _create_my_task(e2e, 100926)
    # The agent's branch is created at the caller's HEAD, so it must exist.
    expect(e2e.run("git rev-parse --verify mngr/my-task", comment="confirm the agent's branch exists")).to_succeed()
    # Give the branch real work to integrate: have the agent commit a new file
    # on its own branch, so the merge below actually advances the caller's HEAD
    # rather than reporting "Already up to date".
    expect(
        e2e.run(
            "mngr exec my-task 'echo agent-change > agent_work.txt && "
            'git add agent_work.txt && git commit -m "agent work"\'',
            comment="have the agent commit work on its branch",
        )
    ).to_succeed()
    # merge the agent's work like normal if the agent is local
    merge_result = e2e.run("git merge mngr/my-task", comment="merge the agent's work")
    expect(merge_result).to_succeed()
    # The caller had no commits of its own beyond the agent's base, so the merge
    # is a fast-forward (not a divergent merge commit).
    expect(merge_result.stdout).to_contain("Fast-forward")
    # The merge must bring the agent's committed file into the caller's tree.
    merged = e2e.run("cat agent_work.txt", comment="verify the agent's work is now present")
    expect(merged).to_succeed()
    expect(merged.stdout).to_contain("agent-change")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_git_merge_agent_branch_creates_merge_commit(e2e: E2eSession) -> None:
    # Same tutorial line as test_git_merge_agent_branch, but exercising the
    # non-fast-forward path: when the caller has also committed locally since
    # the agent branched, "git merge" must reconcile the two diverging
    # histories into a real merge commit (with two parents) rather than a
    # fast-forward.
    e2e.write_tutorial_block("""
        # merge the agent's work like normal if the agent is local:
        git merge mngr/my-task
    """)
    _create_my_task(e2e, 100926)
    # The agent commits its own work on its branch...
    expect(
        e2e.run(
            "mngr exec my-task 'echo agent-change > agent_work.txt && "
            'git add agent_work.txt && git commit -m "agent work"\'',
            comment="have the agent commit work on its branch",
        )
    ).to_succeed()
    # ...while the caller independently commits different work locally, so the
    # two branches diverge and the merge cannot fast-forward.
    expect(
        e2e.run(
            'echo caller-change > caller_work.txt && git add caller_work.txt && '
            'git commit -m "caller work"',
            comment="commit divergent work locally",
        )
    ).to_succeed()
    # merge the agent's work like normal if the agent is local (--no-edit keeps
    # the merge non-interactive by accepting the default merge message).
    merge_result = e2e.run("git merge --no-edit mngr/my-task", comment="merge the agent's work")
    expect(merge_result).to_succeed()
    # Both the agent's and the caller's files must be present after the merge.
    merged_agent = e2e.run("cat agent_work.txt", comment="verify the agent's work is present")
    expect(merged_agent).to_succeed()
    expect(merged_agent.stdout).to_contain("agent-change")
    merged_caller = e2e.run("cat caller_work.txt", comment="verify the caller's work is present")
    expect(merged_caller).to_succeed()
    expect(merged_caller.stdout).to_contain("caller-change")
    # The merge produced a real merge commit: "git rev-list --parents -n 1 HEAD"
    # prints the commit hash followed by all of its parent hashes, so a merge
    # commit yields three tokens (itself + two parents).
    parents = e2e.run("git rev-list --parents -n 1 HEAD", comment="inspect the merge commit's parents")
    expect(parents).to_succeed()
    assert len(parents.stdout.split()) == 3, (
        f"expected a merge commit with two parents, got: {parents.stdout!r}"
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_remove_created_branch_inline(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # when destroying, clean up the branch that was originally created when the agent was created
        mngr destroy my-task --force --remove-created-branch
    """)
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
    # The primary effect of destroy is that the agent itself is gone.
    expect(destroy_result.stdout).to_contain("Successfully destroyed 1 agent(s)")
    # --remove-created-branch reports deleting the branch it created for the agent.
    expect(destroy_result.stdout).to_contain("Deleted branch: mngr/my-task")
    # Verify the concrete effect: the branch is actually gone from the repo.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's created branch was removed",
    )
    expect(branch_after.stdout).to_be_empty()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_destroy_without_remove_created_branch_keeps_branch(e2e: E2eSession) -> None:
    # Unhappy-path counterpart to test_destroy_remove_created_branch_inline,
    # sharing the same tutorial block: it shows that --remove-created-branch is
    # what drives branch removal. Without the flag, a plain destroy leaves the
    # agent's created branch in place.
    e2e.write_tutorial_block("""
        # when destroying, clean up the branch that was originally created when the agent was created
        mngr destroy my-task --force --remove-created-branch
    """)
    _create_my_task(e2e, 100929)
    branch_before = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's created branch exists before destroy",
    )
    expect(branch_before.stdout).to_contain("mngr/my-task")
    # Destroy WITHOUT --remove-created-branch: the agent goes away but the
    # branch must be retained.
    destroy_result = e2e.run(
        "mngr destroy my-task --force",
        comment="destroy without removing the created branch",
    )
    expect(destroy_result).to_succeed()
    # The agent is destroyed, but with no --remove-created-branch flag the
    # branch is left untouched.
    expect(destroy_result.stdout).to_contain("Successfully destroyed 1 agent(s)")
    expect(destroy_result.stdout).not_to_contain("Deleted branch")
    # Verify the concrete effect: the agent's created branch survives.
    branch_after = e2e.run(
        "git branch --list mngr/my-task",
        comment="confirm the agent's created branch was retained",
    )
    expect(branch_after.stdout).to_contain("mngr/my-task")
