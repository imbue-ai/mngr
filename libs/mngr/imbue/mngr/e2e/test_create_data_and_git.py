"""Tests for data sources, projects, and git/branch options from the tutorial."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.rsync
def test_create_with_source_path(e2e: E2eSession, tmp_path: Path) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100082")
    e2e.write_tutorial_block("""
    # by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
    mngr create my-task --from /path/to/some/other/project
    """)
    source_dir = tmp_path / "other_project"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello from source")

    expect(
        e2e.run(
            f"mngr create my-task --from {source_dir} --type {sleep_agent_type} --no-ensure-clean",
            comment="the agent uses the data from its current git repo (if any) or folder, but you can specify a different source",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_project_label(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100083")
    e2e.write_tutorial_block("""
    # similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
    mngr create my-task --project my-project
    """)
    expect(
        e2e.run(
            f"mngr create my-task --project my-project --type {sleep_agent_type} --no-ensure-clean",
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
@pytest.mark.modal
@pytest.mark.rsync
def test_create_with_source_path_no_git(e2e: E2eSession, tmp_path: Path) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100084")
    e2e.write_tutorial_block("""
    # mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder as the source data
    mkdir -p /tmp/my_random_folder
    echo "print('hello world')" > /tmp/my_random_folder/script.py
    mngr create my-task --from /tmp/my_random_folder python -- script.py
    """)
    source_dir = tmp_path / "my_random_folder"
    source_dir.mkdir()
    (source_dir / "script.py").write_text("print('hello world')\n")

    expect(
        e2e.run(
            f"mngr create my-task --from {source_dir} --type {sleep_agent_type} --no-ensure-clean",
            comment="mngr doesn't require git at all--if there's no git repo, it will just use the files from the folder",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_default_branch(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100085")
    e2e.write_tutorial_block("""
    # however, if you do use git, mngr makes that convenient
    # by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
    mngr create my-task
    git branch | grep mngr/my-task
    """)
    expect(
        e2e.run(
            f"mngr create my-task --type {sleep_agent_type} --no-ensure-clean",
            comment="by default, it creates a new git branch for each agent",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the mngr branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_custom_branch_pattern(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100086")
    e2e.write_tutorial_block("""
    # --branch controls branch creation. the default is :mngr/* which creates a new branch named mngr/{agent_name}
    # you can change the pattern (the * is replaced by the agent name):
    mngr create my-task --branch ":feature/*"
    git branch | grep feature/my-task
    """)
    expect(
        e2e.run(
            f"mngr create my-task --branch ':feature/*' --type {sleep_agent_type} --no-ensure-clean",
            comment="you can change the pattern (the * is replaced by the agent name)",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the feature branch was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("feature/my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_base_branch(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100087")
    e2e.write_tutorial_block("""
    # you can also specify a different base branch (instead of the current branch):
    mngr create my-task --branch "main:mngr/*"
    """)
    # First, find out what the current branch is called so we can create a "main" branch
    # The temp_git_repo has a default branch; we just use it as-is since the base branch
    # must exist. We'll use the current branch name as the base.
    current_branch_result = e2e.run(
        "git rev-parse --abbrev-ref HEAD",
        comment="Get current branch name to use as base",
    )
    expect(current_branch_result).to_succeed()
    current_branch = current_branch_result.stdout.strip()

    expect(
        e2e.run(
            f"mngr create my-task --branch '{current_branch}:mngr/*' --type {sleep_agent_type} --no-ensure-clean",
            comment="you can also specify a different base branch (instead of the current branch)",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the branch was created from the base")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("mngr/my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_explicit_branch_name(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100088")
    e2e.write_tutorial_block("""
    # or set the new branch name explicitly:
    mngr create my-task --branch ":feature/my-task"
    """)
    expect(
        e2e.run(
            f"mngr create my-task --branch ':feature/my-task' --type {sleep_agent_type} --no-ensure-clean",
            comment="or set the new branch name explicitly",
        )
    ).to_succeed()

    branch_result = e2e.run("git branch", comment="Check that the exact branch name was created")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain("feature/my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.rsync
def test_create_with_transfer_git_mirror(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100089")
    e2e.write_tutorial_block("""
    # you can create a git mirror instead of a worktree:
    mngr create my-task --transfer=git-mirror
    # git-mirror is used by default for remote agents
    """)
    expect(
        e2e.run(
            f"mngr create my-task --transfer=git-mirror --type {sleep_agent_type} --no-ensure-clean",
            comment="you can create a git mirror instead of a worktree",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.rsync
def test_create_git_mirror_with_existing_branch(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100090")
    e2e.write_tutorial_block("""
    # you can disable new branch creation entirely by omitting the :NEW part (requires --transfer=none or --transfer=git-mirror due to how worktrees work, and --transfer=none implies no new branch):
    mngr create my-task --transfer=git-mirror --branch main
    """)
    current_branch_result = e2e.run(
        "git rev-parse --abbrev-ref HEAD",
        comment="Get current branch name",
    )
    expect(current_branch_result).to_succeed()
    current_branch = current_branch_result.stdout.strip()

    expect(
        e2e.run(
            f"mngr create my-task --transfer=git-mirror --branch {current_branch} --type {sleep_agent_type} --no-ensure-clean",
            comment="you can disable new branch creation entirely by omitting the :NEW part",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_transfer_none(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100091")
    e2e.write_tutorial_block("""
    # you can run the agent in-place (directly in your source directory) without any transfer:
    mngr create my-task --transfer=none
    """)
    expect(
        e2e.run(
            f"mngr create my-task --transfer=none --type {sleep_agent_type} --no-ensure-clean",
            comment="you can run the agent in-place without any transfer",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_from_another_agent(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type("sleep 100092")
    e2e.write_tutorial_block("""
    # you can clone from an existing agent's work directory:
    mngr create my-task --from other-agent
    # (--source is an alias for --from; the format supports agent@host.provider:path)
    """)
    expect(
        e2e.run(
            f"mngr create other-agent --type {sleep_agent_type} --no-ensure-clean",
            comment="Create source agent to clone from",
        )
    ).to_succeed()

    expect(
        e2e.run(
            f"mngr create my-task --from other-agent --type {sleep_agent_type} --no-ensure-clean",
            comment="you can clone from an existing agent's work directory",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify both agents exist")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agent_names = [a["name"] for a in parsed["agents"]]
    assert "other-agent" in agent_names
    assert "my-task" in agent_names
