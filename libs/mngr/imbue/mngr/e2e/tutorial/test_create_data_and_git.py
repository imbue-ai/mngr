"""Tests for data sources, projects, and git/branch options from the tutorial."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# NOTE: This test is intentionally not marked @pytest.mark.modal. Its tutorial
# command (`mngr create --from <path>`) creates a *local* agent, and the only
# modal contact is the incidental discovery `mngr list` performs to look for
# modal-hosted agents (it finds none here). That discovery happens via the
# modal Python SDK inside the `mngr` subprocess, which the resource guard's
# in-process SDK monkeypatch cannot observe -- so a @pytest.mark.modal here
# would always fail the guard's "marked modal but never invoked modal" check.
# Contrast with rsync/tmux, which mngr drives through real binaries that the
# guard's PATH wrapper does track.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_with_source_path(e2e: E2eSession, tmp_path: Path) -> None:
    e2e.write_tutorial_block("""
    # by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
    mngr create my-task --from /path/to/some/other/project
    """)
    source_dir = tmp_path / "other_project"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello from source")

    expect(
        e2e.run(
            f"mngr create my-task --from {source_dir} --type command --no-ensure-clean -- sleep 100082",
            comment="the agent uses the data from its current git repo (if any) or folder, but you can specify a different source",
        )
    ).to_succeed()

    # Scope discovery to the local provider: this test creates a local agent, and
    # the default cross-provider fan-out would otherwise surface unrelated cloud
    # provider errors (e.g. AWS reporting unavailable when no credentials are
    # configured), which `mngr list` reports as a non-zero exit under the default
    # --on-error abort. Sibling tutorial tests use the same --provider local
    # scoping for local-only verification.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the source file was actually transferred to the agent's work directory
    cat_result = e2e.run(
        "mngr exec --agent my-task 'cat hello.txt'",
        comment="Verify source data was transferred to agent work dir",
    )
    expect(cat_result).to_succeed()
    expect(cat_result.stdout).to_contain("hello from source")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_project_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
    mngr create my-task --project my-project
    """)
    expect(
        e2e.run(
            "mngr create my-task --project my-project --type command --no-ensure-clean -- sleep 100083",
            comment="by default the agent is tagged with a project label that matches the name of the current git repo (or folder), but you can specify a different project",
        )
    ).to_succeed()

    # Scope the verification listing to the local provider: the agent created
    # above is local, and `mngr list` defaults to --on-error abort, so an
    # unconfigured remote backend installed in the venv (e.g. the aws plugin
    # with no credentials) would otherwise abort discovery and fail this check
    # for reasons unrelated to the project label being verified here.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify project label is set")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["labels"]["project"] == "my-project"

    # The point of the project label is that it is usable for filtering, so
    # verify the documented behavior end-to-end: `mngr list --project <name>`
    # selects this agent only when the project matches.
    matched_filter = e2e.run(
        "mngr list --provider local --project my-project --format json",
        comment="Filtering by the assigned project label selects the agent",
    )
    expect(matched_filter).to_succeed()
    matched_names = [a["name"] for a in json.loads(matched_filter.stdout)["agents"]]
    assert "my-task" in matched_names

    unmatched_filter = e2e.run(
        "mngr list --provider local --project some-other-project --format json",
        comment="Filtering by a different project label does not select the agent",
    )
    expect(unmatched_filter).to_succeed()
    unmatched_names = [a["name"] for a in json.loads(unmatched_filter.stdout)["agents"]]
    assert "my-task" not in unmatched_names


# NOTE: deliberately not marked @pytest.mark.rsync. This creates a *local*
# command agent, and local source transfers write files directly (shutil), never
# shelling out to rsync (rsync is used only for transfers to a *remote* host --
# see imbue.mngr.hosts.file_upload). Marking rsync here would trip the resource
# guard's "marked rsync but never invoked rsync" check.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_default_project_label(e2e: E2eSession) -> None:
    # Covers the first half of the same tutorial block: when --project is
    # omitted, the agent's project label defaults to the current git repo's
    # folder name (there is no git remote in the test repo, so it falls back to
    # the directory name).
    e2e.write_tutorial_block("""
    # similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
    mngr create my-task --project my-project
    """)
    pwd_result = e2e.run("pwd", comment="Get the current git repo directory")
    expect(pwd_result).to_succeed()
    repo_name = Path(pwd_result.stdout.strip()).name

    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100093",
            comment="by default the agent is tagged with a project label that matches the name of the current git repo (or folder)",
        )
    ).to_succeed()

    # Scope discovery to the local provider where this command agent actually
    # runs. A bare `mngr list` fans out to every registered backend, and any
    # cloud backend that is installed-but-unconfigured (e.g. aws/azure/gcp with
    # no credentials) makes a full enumerate-all discovery fail loudly by
    # design -- which is unrelated to the project-label behavior under test.
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="Verify the default project label matches the repo folder name",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["labels"]["project"] == repo_name


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(120)
def test_create_with_source_path_no_git(e2e: E2eSession, tmp_path: Path) -> None:
    e2e.write_tutorial_block("""
    # mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder as the source data
    mkdir -p /tmp/my_random_folder
    echo "print('hello world')" > /tmp/my_random_folder/script.py
    mngr create my-task --from /tmp/my_random_folder --type command -- python script.py
    """)
    source_dir = tmp_path / "my_random_folder"
    source_dir.mkdir()
    (source_dir / "script.py").write_text("print('hello world')\n")

    expect(
        e2e.run(
            f"mngr create my-task --from {source_dir} --type command --no-ensure-clean -- sleep 100084",
            comment="mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --provider local", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the source file was actually transferred to the agent's work directory
    cat_result = e2e.run(
        'mngr exec my-task "cat script.py"',
        comment="Verify source files were copied to agent work directory",
    )
    expect(cat_result).to_succeed()
    expect(cat_result.stdout).to_contain("hello world")

    # The whole point of this tutorial block is that mngr does not require git:
    # a non-git source folder is copied verbatim, so no agent branch is created.
    branch_result = e2e.run(
        "git branch",
        comment="Verify no git branch was created for a non-git source",
    )
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).not_to_contain("mngr/my-task")

    # The agent's own work directory should not be a git repository either,
    # since it was copied from a plain (non-git) folder.
    is_git_result = e2e.run(
        "mngr exec my-task 'git rev-parse --is-inside-work-tree || echo NO_GIT'",
        comment="Verify the agent work directory is not a git repository",
    )
    expect(is_git_result).to_succeed()
    expect(is_git_result.stdout).to_contain("NO_GIT")


# NOTE: this test is intentionally not marked @pytest.mark.rsync. The default
# transfer for a git source is GIT_WORKTREE, which only rsyncs *extra* files
# (uncommitted/untracked/gitignored) via `_transfer_extra_files`. The e2e
# fixture's repo is clean -- its seeded config lives in gitignored paths -- so
# `git status --porcelain` is empty and rsync is never invoked. A spurious
# rsync mark would fail the resource guard's "marked but never invoked" check.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_default_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # however, if you do use git, mngr makes that convenient
    # by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
    mngr create my-task
    git branch | grep mngr/my-task
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100085",
            comment="by default, it creates a new git branch for each agent",
            # Creation attempts a one-time ttyd install on the host, which can
            # push it past the 30s default under load; allow extra headroom.
            timeout=60.0,
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the mngr branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("mngr/my-task")

    # Verify the agent's worktree is on the new branch
    agent_branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the agent is running on the mngr/my-task branch",
    )
    expect(agent_branch_result).to_succeed()
    expect(agent_branch_result.stdout).to_contain("mngr/my-task")

    # Verify the new branch starts from the same commit as the current branch
    main_commit_result = e2e.run(
        "git rev-parse HEAD",
        comment="Get current branch commit",
    )
    expect(main_commit_result).to_succeed()
    branch_commit_result = e2e.run(
        "git rev-parse mngr/my-task",
        comment="Get mngr/my-task branch commit",
    )
    expect(branch_commit_result).to_succeed()
    assert main_commit_result.stdout.strip() == branch_commit_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_default_branch_distinct_per_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # however, if you do use git, mngr makes that convenient
    # by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
    mngr create my-task
    git branch | grep mngr/my-task
    """)
    # The tutorial's key claim is that a *separate* branch is created for *each*
    # agent so their changes don't conflict. Create two agents and verify each
    # lands on its own distinct branch, both based on the current commit.
    base_commit_result = e2e.run("git rev-parse HEAD", comment="Get current branch commit")
    expect(base_commit_result).to_succeed()
    base_commit = base_commit_result.stdout.strip()

    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100185",
            comment="by default, it creates a new git branch for each agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create other-task --type command --no-ensure-clean -- sleep 100186",
            comment="a second agent gets its own separate branch so their changes don't conflict",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that a distinct branch was created per agent")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("mngr/my-task")
    expect(branch_result.stdout).to_contain("mngr/other-task")

    # Each agent's worktree must be on its own branch, not a shared one.
    first_branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the first agent is on the mngr/my-task branch",
    )
    expect(first_branch_result).to_succeed()
    expect(first_branch_result.stdout).to_contain("mngr/my-task")
    second_branch_result = e2e.run(
        "mngr exec other-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the second agent is on the mngr/other-task branch",
    )
    expect(second_branch_result).to_succeed()
    expect(second_branch_result.stdout).to_contain("mngr/other-task")

    # Both branches are distinct refs that start from the same base commit.
    first_commit_result = e2e.run("git rev-parse mngr/my-task", comment="Get mngr/my-task branch commit")
    expect(first_commit_result).to_succeed()
    second_commit_result = e2e.run("git rev-parse mngr/other-task", comment="Get mngr/other-task branch commit")
    expect(second_commit_result).to_succeed()
    assert first_commit_result.stdout.strip() == base_commit
    assert second_commit_result.stdout.strip() == base_commit


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_custom_branch_pattern(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # --branch controls branch creation. The format is "BASE:NEW", where BASE is the branch to start from and NEW is the branch to create.
    # omitting BASE (i.e. starting with ":") uses the current branch. The * in NEW is replaced by the agent name.
    # the default is ":mngr/*", which creates a new branch named mngr/{agent_name} off the current branch.
    # you can change the pattern:
    mngr create my-task --branch ":feature/*"
    git branch | grep feature/my-task
    """)
    expect(
        e2e.run(
            "mngr create my-task --branch ':feature/*' --type command --no-ensure-clean -- sleep 100086",
            comment="you can change the pattern (the * is replaced by the agent name)",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the feature branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("feature/my-task")

    # Verify the agent's worktree is actually on the feature branch
    worktree_branch = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the agent worktree is on the feature branch",
    )
    expect(worktree_branch).to_succeed()
    expect(worktree_branch.stdout).to_contain("feature/my-task")

    # Omitting BASE (leading ":") means the new branch starts from the current
    # branch, so feature/my-task should point at the same commit as HEAD.
    head_commit_result = e2e.run("git rev-parse HEAD", comment="Get current branch commit")
    expect(head_commit_result).to_succeed()
    feature_commit_result = e2e.run(
        "git rev-parse feature/my-task",
        comment="Verify the feature branch is based on the current branch",
    )
    expect(feature_commit_result).to_succeed()
    assert feature_commit_result.stdout.strip() == head_commit_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_base_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also specify a different base branch (instead of the current branch):
    mngr create my-task --branch "main:mngr/*"
    """)
    # Record main's commit, then switch to a diverged branch so we can verify
    # that the agent branch is based on main (not on the current branch).
    main_rev_result = e2e.run(
        "git rev-parse HEAD",
        comment="Record the commit that main points to",
    )
    expect(main_rev_result).to_succeed()
    main_rev = main_rev_result.stdout.strip()

    expect(
        e2e.run(
            "git checkout -b diverged && git commit --allow-empty -m 'diverge'",
            comment="Create a diverged branch with an extra commit",
        )
    ).to_succeed()

    expect(
        e2e.run(
            "mngr create my-task --branch 'main:mngr/*' --type command --no-ensure-clean -- sleep 100087",
            comment="you can also specify a different base branch (instead of the current branch)",
        )
    ).to_succeed()

    # Verify the branch exists
    branch_result = e2e.run("git branch", comment="Check that the branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("mngr/my-task")

    # Verify the agent branch points to the main commit, not the diverged commit
    agent_rev_result = e2e.run(
        "git rev-parse mngr/my-task",
        comment="Verify agent branch is based on main, not the current diverged branch",
    )
    expect(agent_rev_result).to_succeed()
    assert agent_rev_result.stdout.strip() == main_rev

    # Verify the agent's own worktree is checked out on the new branch (not just
    # that the branch exists in the host repo). `git worktree list` is a fast
    # local query that shows each worktree's checked-out branch.
    worktree_list = e2e.run(
        "git worktree list --porcelain",
        comment="Verify the agent worktree is checked out on the mngr/my-task branch",
    )
    expect(worktree_list).to_succeed()
    expect(worktree_list.stdout).to_contain("refs/heads/mngr/my-task")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_create_with_nonexistent_base_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also specify a different base branch (instead of the current branch):
    mngr create my-task --branch "main:mngr/*"
    """)
    # Unhappy path for the same tutorial block: if the named base branch does not
    # exist, creation must fail cleanly and must not leave a dangling agent branch.
    create_result = e2e.run(
        "mngr create my-task --branch 'nonexistent-base:mngr/*' --type command --no-ensure-clean -- sleep 100088",
        comment="specifying a base branch that does not exist should fail",
    )
    expect(create_result).to_fail()
    # The failure must be *because* the base branch does not exist, not some
    # unrelated error (e.g. a malformed config). Assert the missing base branch
    # name surfaces in the error so the test cannot silently pass for the wrong
    # reason.
    expect(create_result.stderr).to_contain("nonexistent-base")

    branch_result = e2e.run("git branch", comment="Check that no agent branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).not_to_contain("mngr/my-task")

    # "Fail cleanly" also means no dangling worktree is left behind: the create
    # fails while running `git worktree add`, so the source repo must still have
    # only its original worktree (no half-created mngr/my-task worktree). This is
    # a fast, local check that does not depend on remote provider discovery.
    worktree_result = e2e.run(
        "git worktree list",
        comment="Check that no dangling worktree was left behind",
    )
    expect(worktree_result).to_succeed()
    expect(worktree_result.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_explicit_branch_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or set the new branch name explicitly:
    mngr create my-task --branch ":feature/my-task"
    """)
    expect(
        e2e.run(
            "mngr create my-task --branch ':feature/my-task' --type command --no-ensure-clean -- sleep 100088",
            comment="or set the new branch name explicitly",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the exact branch name was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("feature/my-task")

    # The explicit name is taken literally (no "*" to substitute), so the
    # default ":mngr/*" pattern must NOT have produced an mngr/my-task branch.
    assert "mngr/my-task" not in branch_result.stdout

    # Verify the agent's worktree is actually on the explicit branch
    agent_branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the agent worktree is on the explicit branch",
    )
    expect(agent_branch_result).to_succeed()
    expect(agent_branch_result.stdout).to_contain("feature/my-task")

    # The new branch should start from the current branch's commit.
    head_commit_result = e2e.run("git rev-parse HEAD", comment="Get current branch commit")
    expect(head_commit_result).to_succeed()
    explicit_commit_result = e2e.run(
        "git rev-parse feature/my-task",
        comment="Get the explicit branch commit",
    )
    expect(explicit_commit_result).to_succeed()
    assert explicit_commit_result.stdout.strip() == head_commit_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
# NOTE: unlike the sibling create tests, this one is NOT marked @pytest.mark.rsync:
# `--transfer=git-mirror` clones the repo over git rather than rsyncing the work
# tree, so rsync is never invoked and the resource guard would flag the mark.
# Agent creation (provisioning, git-mirror clone, ttyd install attempt) can exceed
# the default 10s per-test timeout, so allow extra headroom (matching the sibling
# create tests in this file).
@pytest.mark.timeout(120)
def test_create_git_mirror_with_existing_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can disable new branch creation entirely by omitting the :NEW part:
    mngr create my-task --branch main
    # this checks out the existing branch in the worktree (or copy) without creating a new one
    """)
    current_branch_result = e2e.run(
        "git rev-parse --abbrev-ref HEAD",
        comment="Get current branch name",
    )
    expect(current_branch_result).to_succeed()
    current_branch = current_branch_result.stdout.strip()

    expect(
        e2e.run(
            f"mngr create my-task --transfer=git-mirror --branch {current_branch} --type command --no-ensure-clean -- sleep 100090",
            comment="disable new branch creation by omitting the :NEW part (using git-mirror since worktrees cannot share branches)",
        )
    ).to_succeed()

    # Scope discovery to the local provider: the agent runs on localhost, and
    # querying every registered backend would make `mngr list` exit non-zero
    # whenever an unconfigured cloud provider (e.g. AWS) is enabled-but-
    # uncredentialed in the test environment.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the agent is on the expected branch (not a newly created one).
    # The mngr exec verification calls use a generous timeout because each one
    # re-runs agent/provider discovery, which can exceed the 30s default when the
    # machine is under load (e.g. running release tests back-to-back locally).
    branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify agent is on the existing branch, not a new one",
        timeout=60.0,
    )
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(current_branch)

    # The existing branch must be checked out at the same commit it points to in
    # the source repo: git-mirror mirrors the repo, so the agent's branch is not
    # just same-named but actually at the source branch's HEAD.
    source_commit_result = e2e.run(
        f"git rev-parse {current_branch}",
        comment="Get the source repo's commit for the existing branch",
    )
    expect(source_commit_result).to_succeed()
    mirror_commit_result = e2e.run(
        "mngr exec my-task 'git rev-parse HEAD'",
        comment="Verify the agent mirror is checked out at the source branch's commit",
        timeout=60.0,
    )
    expect(mirror_commit_result).to_succeed()
    # `mngr exec` appends a status line to stdout, so match the commit by
    # substring rather than exact equality.
    expect(mirror_commit_result.stdout).to_contain(source_commit_result.stdout.strip())

    # The whole point of omitting the :NEW part is that no new branch is created:
    # neither the source repo nor the agent's mirror should have a mngr/my-task branch.
    source_branches = e2e.run("git branch", comment="Verify no new mngr/* branch in the source repo")
    expect(source_branches).to_succeed()
    assert "mngr/my-task" not in source_branches.stdout
    mirror_branches = e2e.run(
        "mngr exec my-task 'git branch'",
        comment="Verify no new mngr/* branch in the agent's git mirror",
        timeout=60.0,
    )
    expect(mirror_branches).to_succeed()
    assert "mngr/my-task" not in mirror_branches.stdout


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_with_transfer_none(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can run the agent in-place (directly in your source directory) without any transfer:
    mngr create my-task --transfer=none
    # mngr defaults to creating a new worktree for each agent because the whole point of mngr is to let you run multiple agents in parallel.
    # without creating a new worktree for each, they will make conflicting changes with one another.
    """)
    expect(
        e2e.run(
            "mngr create my-task --transfer=none --type command --no-ensure-clean -- sleep 100091",
            comment="you can run the agent in-place without any transfer",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify agent runs in-place")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1

    # With --transfer=none, the agent should run in the source directory (in-place)
    pwd_result = e2e.run("pwd", comment="Get the source directory path")
    expect(pwd_result).to_succeed()
    source_dir = pwd_result.stdout.strip()
    assert matching[0]["work_dir"] == source_dir

    # Verify the agent's actual runtime working directory is the source directory,
    # not just the work_dir reported by `mngr list` (i.e. it really runs in-place).
    exec_pwd_result = e2e.run(
        "mngr exec my-task pwd",
        comment="Verify the agent is actually running in the source directory",
    )
    expect(exec_pwd_result).to_succeed()
    expect(exec_pwd_result.stdout).to_contain(source_dir)

    # No new branch should be created (--transfer=none implies no new branch)
    branch_result = e2e.run("git branch", comment="Verify no mngr/* branch was created")
    expect(branch_result).to_succeed()
    assert "mngr/my-task" not in branch_result.stdout


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(240)
def test_create_from_another_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can clone from an existing agent's work directory:
    mngr create my-task --from other-agent
    # (--source is an alias for --from; the format supports agent@host.provider:path)
    """)
    expect(
        e2e.run(
            "mngr create other-agent --type command --no-ensure-clean -- sleep 100092",
            comment="Create source agent to clone from",
        )
    ).to_succeed()

    # Drop a distinctive marker file into the source agent's work directory so
    # we can later confirm the clone actually copied the source agent's
    # directory contents (the whole point of --from <agent>).
    expect(
        e2e.run(
            "mngr exec other-agent 'echo CLONE_MARKER_9f3a > cloned_marker.txt'",
            comment="Write a marker file into the source agent's work dir",
        )
    ).to_succeed()

    # Pin a distinct sleep value for the cloned agent so leaked processes can be traced back to this call.
    # Resolving `--from <agent>` makes mngr discover the source agent across all
    # configured providers (modal/vultr/docker), which can exceed the 30s default
    # command timeout when the machine is under load (e.g. running release tests
    # back-to-back locally), so this discovery-heavy create gets a generous timeout.
    expect(
        e2e.run(
            "mngr create my-task --from other-agent --type command --no-ensure-clean -- sleep 100122",
            comment="you can clone from an existing agent's work directory",
            timeout=120.0,
        )
    ).to_succeed()

    # Both agents run on the local provider: no host/provider was specified, so
    # create uses the local provider's default host. Scope the listing to
    # `--provider local` so verification is not aborted by unrelated providers
    # that are merely enabled-but-unavailable in the test environment. With the
    # default `--on-error abort`, `mngr list` exits non-zero if *any* enabled
    # backend raises during discovery -- e.g. a stopped Docker daemon, or an
    # unconfigured cloud backend (aws/azure/gcp/vultr) whose plugin is installed
    # in the monorepo venv. Those are irrelevant to this test, which only needs
    # to confirm the two local agents it just created.
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="Verify both agents exist (scoped to the local provider they run on)",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "other-agent" in agent_names
    assert "my-task" in agent_names

    # Verify both agents have separate work directories but share the same host
    agents_by_name = {a["name"]: a for a in parsed["agents"]}
    assert agents_by_name["other-agent"]["work_dir"] != agents_by_name["my-task"]["work_dir"]
    assert agents_by_name["my-task"]["host"]["name"] == agents_by_name["other-agent"]["host"]["name"]

    # Verify the cloned agent got its own branch
    assert agents_by_name["my-task"]["initial_branch"] == "mngr/my-task"

    # Verify the clone actually copied the source agent's work-dir contents:
    # the marker file written into other-agent must be present in my-task.
    marker_result = e2e.run(
        "mngr exec my-task 'cat cloned_marker.txt'",
        comment="Verify the source agent's work-dir contents were cloned",
        timeout=60.0,
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("CLONE_MARKER_9f3a")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_from_another_agent_source_alias(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can clone from an existing agent's work directory:
    mngr create my-task --from other-agent
    # (--source is an alias for --from; the format supports agent@host.provider:path)
    """)
    # The tutorial block explicitly documents that `--source` is an alias for
    # `--from`. This test exercises that alias path (the sibling
    # test_create_from_another_agent covers `--from`) and verifies it clones the
    # source agent's work-dir contents identically.
    expect(
        e2e.run(
            "mngr create other-agent --type command --no-ensure-clean -- sleep 100192",
            comment="Create source agent to clone from",
        )
    ).to_succeed()

    # Drop a distinctive marker file into the source agent's work directory so we
    # can later confirm `--source` actually copied the source agent's contents.
    expect(
        e2e.run(
            "mngr exec other-agent 'echo SOURCE_ALIAS_MARKER_b71c > cloned_marker.txt'",
            comment="Write a marker file into the source agent's work dir",
        )
    ).to_succeed()

    # Resolving a bare ``--source <agent-name>`` (no host/provider) forces a full
    # provider scan, since mngr cannot know which provider hosts the source agent
    # without looking. In the dev environment that scan constructs every
    # registered cloud backend (aws, azure, gcp, ...), whose credential/metadata
    # lookups are slow, so allow extra headroom beyond the 30s default.
    expect(
        e2e.run(
            "mngr create my-task --source other-agent --type command --no-ensure-clean -- sleep 100222",
            comment="--source is an alias for --from",
            timeout=90.0,
        )
    ).to_succeed()

    # Both agents run on the default (local) provider, so scope discovery to it.
    # The dev environment registers credential-requiring cloud backends (aws,
    # azure, gcp, ...) that an unconfigured `mngr list` would enumerate and abort
    # on; `--provider local` keeps the verification focused on the agents we
    # actually created, matching the pattern used by the other e2e tutorial tests.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify both agents exist")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents_by_name = {a["name"]: a for a in parsed["agents"]}
    assert "other-agent" in agents_by_name
    assert "my-task" in agents_by_name

    # The clone landed on its own branch and a distinct work dir, just like --from.
    assert agents_by_name["my-task"]["initial_branch"] == "mngr/my-task"
    assert agents_by_name["other-agent"]["work_dir"] != agents_by_name["my-task"]["work_dir"]

    # Verify `--source` actually copied the source agent's work-dir contents.
    marker_result = e2e.run(
        "mngr exec my-task 'cat cloned_marker.txt'",
        comment="Verify the source agent's work-dir contents were cloned via --source",
    )
    expect(marker_result).to_succeed()
    expect(marker_result.stdout).to_contain("SOURCE_ALIAS_MARKER_b71c")
