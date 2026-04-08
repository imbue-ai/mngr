"""Tests for data sources, projects, and git/branch options from the tutorial."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
def test_create_with_source_path(e2e: E2eSession, tmp_path: Path) -> None:
    e2e.write_tutorial_block("""
    # by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
    mngr create my-task --source-path /path/to/some/other/project
    """)
    source_dir = tmp_path / "other_project"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello from source")

    expect(
        e2e.run(
            f"mngr create my-task --source-path {source_dir} --command 'sleep 99999' --no-ensure-clean",
            comment="the agent uses the data from its current git repo (if any) or folder, but you can specify a different source",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
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
def test_create_with_project_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
    mngr create my-task --project my-project
    """)
    expect(
        e2e.run(
            "mngr create my-task --project my-project --command 'sleep 99999' --no-ensure-clean",
            comment="by default the agent is tagged with a project label that matches the name of the current git repo (or folder), but you can specify a different project",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify project label is set")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["labels"]["project"] == "my-project"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
def test_create_with_source_path_no_git(e2e: E2eSession, tmp_path: Path) -> None:
    e2e.write_tutorial_block("""
    # mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder as the source data
    mkdir -p /tmp/my_random_folder
    echo "print('hello world')" > /tmp/my_random_folder/script.py
    mngr create my-task --source-path /tmp/my_random_folder --command python -- script.py
    """)
    source_dir = tmp_path / "my_random_folder"
    source_dir.mkdir()
    (source_dir / "script.py").write_text("print('hello world')\n")

    expect(
        e2e.run(
            f"mngr create my-task --source-path {source_dir} --command 'sleep 99999' --no-ensure-clean",
            comment="mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the source file was actually transferred to the agent's work directory
    cat_result = e2e.run(
        'mngr exec my-task "cat script.py"',
        comment="Verify source files were copied to agent work directory",
    )
    expect(cat_result).to_succeed()
    expect(cat_result.stdout).to_contain("hello world")


@pytest.mark.release
@pytest.mark.tmux
def test_create_default_branch(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # however, if you do use git, mngr makes that convenient
    # by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
    mngr create my-task
    git branch | grep mngr/my-task
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="by default, it creates a new git branch for each agent",
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
            "mngr create my-task --branch ':feature/*' --command 'sleep 99999' --no-ensure-clean",
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


@pytest.mark.release
@pytest.mark.tmux
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
            "mngr create my-task --branch 'main:mngr/*' --command 'sleep 99999' --no-ensure-clean",
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


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_explicit_branch_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or set the new branch name explicitly:
    mngr create my-task --branch ":feature/my-task"
    """)
    expect(
        e2e.run(
            "mngr create my-task --branch ':feature/my-task' --command 'sleep 99999' --no-ensure-clean",
            comment="or set the new branch name explicitly",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the exact branch name was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("feature/my-task")

    # Verify the agent's worktree is actually on the explicit branch
    agent_branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify the agent worktree is on the explicit branch",
    )
    expect(agent_branch_result).to_succeed()
    expect(agent_branch_result.stdout).to_contain("feature/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
def test_create_with_transfer_git_mirror(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can create a git mirror instead of a worktree:
    mngr create my-task --transfer=git-mirror
    # git-mirror is used by default for remote agents
    """)
    expect(
        e2e.run(
            "mngr create my-task --transfer=git-mirror --command 'sleep 99999' --no-ensure-clean",
            comment="you can create a git mirror instead of a worktree",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the agent has a real .git directory (clone), not a .git file (worktree)
    git_check = e2e.run(
        "mngr exec my-task 'test -d .git && echo IS_DIR || echo IS_FILE'",
        comment="Verify .git is a directory (clone) not a file (worktree)",
    )
    expect(git_check).to_succeed()
    expect(git_check.stdout).to_contain("IS_DIR")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
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
            f"mngr create my-task --transfer=git-mirror --branch {current_branch} --command 'sleep 99999' --no-ensure-clean",
            comment="disable new branch creation by omitting the :NEW part (using git-mirror since worktrees cannot share branches)",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the agent is on the expected branch (not a newly created one)
    branch_result = e2e.run(
        "mngr exec my-task 'git rev-parse --abbrev-ref HEAD'",
        comment="Verify agent is on the existing branch, not a new one",
    )
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(current_branch)


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
            "mngr create my-task --transfer=none --command 'sleep 99999' --no-ensure-clean",
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

    # No new branch should be created (--transfer=none implies no new branch)
    branch_result = e2e.run("git branch", comment="Verify no mngr/* branch was created")
    expect(branch_result).to_succeed()
    assert "mngr/my-task" not in branch_result.stdout


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_shallow_depth(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can make a shallow clone for faster setup:
    mngr create my-task --depth 1
    # (--shallow-since clones since a specific date instead)
    """)
    expect(
        e2e.run(
            "mngr create my-task --depth 1 --command 'sleep 99999' --no-ensure-clean --exclude-unclean",
            comment="you can make a shallow clone for faster setup",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the clone is actually shallow
    shallow_check = e2e.run(
        "mngr exec my-task 'git rev-parse --is-shallow-repository'",
        comment="Verify the clone is shallow",
    )
    expect(shallow_check).to_succeed()
    expect(shallow_check.stdout).to_contain("true")


@pytest.mark.release
@pytest.mark.tmux
def test_create_from_another_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can clone from an existing agent's work directory:
    mngr create my-task --from other-agent
    # (--source, --source-agent, and --source-host are alternative forms for more specific control)
    """)
    expect(
        e2e.run(
            "mngr create other-agent --command 'sleep 99999' --no-ensure-clean",
            comment="Create source agent to clone from",
        )
    ).to_succeed()

    expect(
        e2e.run(
            "mngr create my-task --from other-agent --command 'sleep 99999' --no-ensure-clean",
            comment="you can clone from an existing agent's work directory",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify both agents exist")
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
