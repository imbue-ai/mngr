"""Tests for the PROJECTS tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
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
@pytest.mark.timeout(120)
@pytest.mark.timeout(300)
def test_list_current_project_only_filters_to_named_project(e2e: E2eSession) -> None:
    # Shares the PROJECTS tutorial block with test_list_current_project_only, but
    # verifies the actual filtering effect: with agents tagged across two
    # projects, `mngr list --project my-project` returns only the matching one.
    e2e.write_tutorial_block("""
        # agents inherit the project from the directory where you run mngr create.
        # the project is typically the name of the git repo.
        # list agents for the current project only
        mngr list --project my-project
    """)
    # Create two command agents tagged with different projects.
    expect(
        e2e.run(
            "mngr create agent-in-mine --project my-project --type command --no-ensure-clean --no-connect -- sleep 100920",
            comment="agents inherit the project from the directory, or you can tag one explicitly",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create agent-in-other --project other-project --type command --no-ensure-clean --no-connect -- sleep 100921",
            comment="create another agent tagged with a different project",
        )
    ).to_succeed()
    # Sanity check: an unfiltered list shows both agents, so the filter assertion
    # below is meaningful (it isn't passing just because a create silently failed).
    all_agents = e2e.run("mngr list", comment="list all agents across projects")
    expect(all_agents).to_succeed()
    expect(all_agents.stdout).to_contain("agent-in-mine")
    expect(all_agents.stdout).to_contain("agent-in-other")
    # list agents for the current project only
    filtered = e2e.run("mngr list --project my-project", comment="list agents for the current project only")
    expect(filtered).to_succeed()
    expect(filtered.stdout).to_contain("agent-in-mine")
    expect(filtered.stdout).not_to_contain("agent-in-other")


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
    # Confirm the agent is discoverable under the explicitly-requested project.
    # Scope discovery to the local provider so the assertion does not depend on
    # remote (modal) state, which differs between local and CI runs. The project
    # is stored on the labels.project field, so display it to verify the actual
    # stored value.
    list_result = e2e.run(
        'mngr list --provider local --project other-project --fields "name,labels.project"',
        comment="confirm the agent is tagged with the explicit project",
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")
    expect(list_result.stdout).to_contain("other-project")
    # The agent must be tagged with that specific project and no other: filtering
    # by an unrelated project must exclude it (guards against the --project filter
    # being a no-op that would list every agent regardless of its label).
    unrelated_result = e2e.run(
        "mngr list --provider local --project unrelated-project",
        comment="confirm the agent is not tagged with an unrelated project",
    )
    expect(unrelated_result).to_succeed()
    expect(unrelated_result.stdout).not_to_contain("my-task")


@pytest.mark.release
def test_list_filter_project_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    # No agents exist in this fresh environment, so the modal provider is
    # skipped entirely (it only bootstraps a Modal environment on the create
    # path), which is why this test is intentionally not marked @pytest.mark.modal.
    result = e2e.run(
        "mngr list --include 'project == \"my-project\"'",
        comment="filter agents by project using CEL expressions",
    )
    expect(result).to_succeed()
    # The CEL expression parses and evaluates cleanly: with no matching agents
    # the filtered list is empty rather than erroring on the expression.
    expect(result.stdout).to_contain("No agents found")


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
    result = e2e.run("mngr list --project .", comment="list agents for the current project")
    expect(result).to_succeed()
    # With no agents created yet, "." resolves cleanly to the current project and lists
    # nothing -- it must not error out on the literal "." token.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_list_project_dot_filters_to_current_project(e2e: E2eSession) -> None:
    # Shares the "mngr list --project ." tutorial block; verifies that "." actually
    # expands to the *current* project rather than acting as a match-anything wildcard.
    e2e.write_tutorial_block("""
        # the literal "." is expanded to the current project (derived from your git worktree
        # root's remote origin, falling back to its source-repo dir name (for worktrees) or
        # folder name, so it stays correct from any subdirectory), so this lists agents for
        # the project you're currently in:
        mngr list --project .
        # this also works for "mngr kanpan --project ."
    """)
    # An agent created without --project inherits the current project from the worktree.
    expect(
        e2e.run(
            "mngr create dot-agent --type command --no-ensure-clean --no-connect -- sleep 100929",
            comment="create an agent in the current project",
        )
    ).to_succeed()
    # "." must resolve to the current project, so the agent shows up.
    listed = e2e.run("mngr list --project .", comment="list agents for the current project")
    expect(listed).to_succeed()
    expect(listed.stdout).to_contain("dot-agent")
    # A different project must exclude it -- proving "." is not a match-anything wildcard.
    other = e2e.run(
        "mngr list --project some-other-project",
        comment="agents from a different project are excluded",
    )
    expect(other).to_succeed()
    expect(other.stdout).not_to_contain("dot-agent")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_list_project_field(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # see which projects have agents by looking at the project field
        mngr list --fields "name,project,state"
    """)
    # The tutorial block is about *seeing which projects have agents*, so an
    # agent tagged with a known project must exist first -- otherwise the
    # project column has nothing to show. Creating it on Modal (with an
    # explicit project) also drives the Modal CLI via environment_create during
    # provider init, satisfying the modal resource guard.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --project my-project --type command"
            " --no-connect --no-ensure-clean -- sleep 100929",
            comment="create a Modal agent tagged with an explicit project",
            timeout=120.0,
        )
    ).to_succeed()
    # see which projects have agents by looking at the project field
    result = e2e.run('mngr list --fields "name,project,state"', comment="see which projects have agents")
    expect(result).to_succeed()
    # the agent's name and its project must both surface (the project column is the
    # whole point of this tutorial block -- it resolves the "project" short field to
    # the labels.project value)
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("my-project")
