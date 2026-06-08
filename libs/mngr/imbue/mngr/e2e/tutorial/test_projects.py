"""Tests for the PROJECTS tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.modal
def test_list_current_project_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # agents inherit the project from the directory where you run mngr create.
        # the project is typically the name of the git repo.
        # list agents for the current project only
        mngr list --project my-project
    """)
    expect(e2e.run("mngr list --project my-project", comment="list agents for the current project only")).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_explicit_project(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create an agent explicitly tagged with a different project
        mngr create my-task --project other-project
    """)
    expect(
        e2e.run(
            "mngr create my-task --project other-project --type command --no-ensure-clean --no-connect -- sleep 100910",
            comment="create an agent explicitly tagged with a different project",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_filter_project_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    expect(
        e2e.run(
            "mngr list --include 'project == \"my-project\"'",
            comment="filter agents by project using CEL expressions",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_project_dot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # the literal "." is expanded to the current project (derived from your git worktree
        # root's remote origin, falling back to its source-repo dir name (for worktrees) or
        # folder name, so it stays correct from any subdirectory), so this lists agents for
        # the project you're currently in:
        mngr list --project .
        # this also works for "mngr kanpan --project ."
    """)
    expect(e2e.run("mngr list --project .", comment="list agents for the current project")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_list_project_field(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see which projects have agents by looking at the project field
        mngr list --fields "name,project,state"
    """)
    expect(e2e.run('mngr list --fields "name,project,state"', comment="see which projects have agents")).to_succeed()
