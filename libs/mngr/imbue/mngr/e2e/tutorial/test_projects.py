"""Tests for the PROJECTS tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(360)
def test_list_current_project_only(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # agents inherit the project from the directory where you run mngr create.
        # the project is typically the name of the git repo.
        # list agents for the current project only
        mngr list --project my-project
    """)
    # Seed an agent explicitly tagged with "my-project" so the project filter
    # below has something to match. A lightweight command agent (just sleeps) is
    # enough; creating it on Modal invokes the Modal CLI via environment_create
    # during provider initialization, which satisfies the @pytest.mark.modal
    # resource guard. (A pure `mngr list` against an empty environment only
    # performs read-only Modal discovery inside the subprocess, which the guard
    # cannot observe.)
    expect(
        e2e.run(
            "mngr create project-filter-agent --project my-project --provider modal "
            "--type command --no-ensure-clean --no-connect -- sleep 100910",
            comment="seed an agent tagged with the project to be filtered for",
            timeout=180.0,
        )
    ).to_succeed()

    # The tutorial command: list agents for a single project. The seeded agent
    # belongs to "my-project", so it must appear.
    result = e2e.run(
        "mngr list --project my-project", comment="list agents for the current project only", timeout=60.0
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("project-filter-agent")

    # Filtering must be exclusive: an unrelated project shows none of our agents.
    other = e2e.run(
        "mngr list --project some-other-project",
        comment="a different project does not include this project's agents",
        timeout=60.0,
    )
    expect(other).to_succeed()
    expect(other.stdout).not_to_contain("project-filter-agent")


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
def test_list_filter_project_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    result = e2e.run(
        "mngr list --include 'project == \"my-project\"'",
        comment="filter agents by project using CEL expressions",
    )
    expect(result).to_succeed()
    # The CEL expression is accepted and evaluated; with no agents in this fresh
    # environment the project filter matches nothing.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_filter_invalid_cel(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: a syntactically invalid CEL
    # expression must be rejected with a clear error rather than silently
    # ignored or crashing with a traceback.
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    result = e2e.run(
        "mngr list --include 'project =='",
        comment="reject a syntactically invalid CEL expression",
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("Invalid include filter expression")


@pytest.mark.release
def test_list_project_dot(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # the literal "." is expanded to the current project (derived from your git worktree
        # root's remote origin, falling back to its source-repo dir name (for worktrees) or
        # folder name, so it stays correct from any subdirectory), so this lists agents for
        # the project you're currently in:
        mngr list --project .
        # this also works for "mngr kanpan --project ."
    """)
    # The "." must be accepted and expanded to the current project (rather than
    # rejected or treated as a literal project named "."), yielding a clean
    # listing. The fresh e2e environment has no agents, so the expanded
    # current-project filter resolves to an empty result.
    result = e2e.run("mngr list --project .", comment="list agents for the current project")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_project_field(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see which projects have agents by looking at the project field
        mngr list --fields "name,project,state"
    """)
    # Create an agent tagged with a known project so the project field has a
    # concrete value to display (an empty agent list would make "looking at the
    # project field" meaningless).
    expect(
        e2e.run(
            "mngr create my-task --project my-project --type command --no-ensure-clean --no-connect -- sleep 100910",
            comment="create an agent tagged with a known project",
        )
    ).to_succeed()
    # see which projects have agents by looking at the project field
    result = e2e.run('mngr list --fields "name,project,state"', comment="see which projects have agents")
    expect(result).to_succeed()
    # the listing must show the agent under its project, i.e. the project field
    # is populated from the agent's project label rather than rendered empty.
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("my-project")
