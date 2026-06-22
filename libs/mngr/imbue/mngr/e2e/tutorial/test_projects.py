"""Tests for the PROJECTS tutorial section."""

import json

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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_create_with_explicit_project(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create an agent explicitly tagged with a different project
        mngr create my-task --project other-project
    """)
    expect(
        e2e.run(
            "mngr create my-task --project other-project --type command --no-ensure-clean --no-connect -- sleep 100910",
            comment="create an agent explicitly tagged with a different project",
            timeout=120.0,
        )
    ).to_succeed()

    # The point of --project is to override the project that would otherwise be
    # derived from the current git worktree. Verify the created agent is actually
    # tagged with the explicit project rather than the directory-derived one.
    # Scope the listing to the local provider: the agent was created locally, and
    # restricting discovery avoids contacting unconfigured remote providers (e.g.
    # AWS), whose unreachability would otherwise make `mngr list` exit non-zero.
    result = e2e.run(
        'mngr list --provider local --fields "name,project"',
        comment="confirm the agent is tagged with the explicit project",
        timeout=60.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("other-project")

    # The explicit project must take effect exclusively: filtering by it shows
    # the agent, while filtering by an unrelated project does not.
    in_project = e2e.run(
        "mngr list --provider local --project other-project",
        comment="the agent appears when filtering by its explicit project",
        timeout=60.0,
    )
    expect(in_project).to_succeed()
    expect(in_project.stdout).to_contain("my-task")

    not_in_project = e2e.run(
        "mngr list --provider local --project unrelated-project",
        comment="the agent does not appear under an unrelated project",
        timeout=60.0,
    )
    expect(not_in_project).to_succeed()
    expect(not_in_project.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_list_filter_project_cel(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    # Scope discovery to the always-available local provider. The e2e fixture
    # leaves several cloud provider backends enabled (aws, azure, gcp, vultr, ...)
    # but unconfigured, so an unscoped `mngr list` aborts with a non-zero exit
    # when any of them is unreachable -- behavior orthogonal to the CEL filtering
    # this block demonstrates. `--provider local` keeps the `--include` CEL path
    # exercised while making the command deterministic across environments. Even
    # local-only discovery pays the mngr/plugin cold-start cost (tens of seconds),
    # so allow more than the default function/subprocess timeout.
    result = e2e.run(
        "mngr list --include 'project == \"my-project\"' --provider local",
        comment="filter agents by project using CEL expressions",
        timeout=90.0,
    )
    expect(result).to_succeed()
    # The CEL expression is accepted and evaluated; with no agents in this fresh
    # environment the project filter matches nothing.
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_filter_project_cel_discriminates(e2e: E2eSession) -> None:
    # Happy path for the same tutorial block, but verifying that the CEL filter
    # actually discriminates by project rather than merely being accepted: a
    # matching expression includes the seeded agent and a non-matching one
    # excludes it.
    e2e.write_tutorial_block("""
        # filter agents by project using CEL expressions
        mngr list --include 'project == "my-project"'
    """)
    # Seed a local command agent tagged with the project under test. Local keeps
    # the test fast and free of remote-provider dependencies (see the comment in
    # test_list_filter_project_cel for why discovery is scoped to local).
    expect(
        e2e.run(
            "mngr create cel-filter-agent --project my-project --provider local "
            "--type command --no-ensure-clean --no-connect -- sleep 100993",
            comment="seed a local agent tagged with the project to filter for",
            timeout=120.0,
        )
    ).to_succeed()

    # A CEL expression matching the agent's project must include it.
    matching = e2e.run(
        "mngr list --include 'project == \"my-project\"' --provider local --format json",
        comment="filter agents by project using CEL expressions",
        timeout=90.0,
    )
    expect(matching).to_succeed()
    matching_names = [agent["name"] for agent in json.loads(matching.stdout)["agents"]]
    assert "cel-filter-agent" in matching_names, matching_names

    # A CEL expression for a different project must exclude it: the filter is
    # genuinely evaluated, not ignored.
    non_matching = e2e.run(
        "mngr list --include 'project == \"some-other-project\"' --provider local --format json",
        comment="a non-matching CEL expression excludes the agent",
        timeout=90.0,
    )
    expect(non_matching).to_succeed()
    non_matching_names = [agent["name"] for agent in json.loads(non_matching.stdout)["agents"]]
    assert "cel-filter-agent" not in non_matching_names, non_matching_names


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
        timeout=60.0,
    )
    expect(result).to_fail()
    expect(result.stdout + result.stderr).to_contain("Invalid include filter expression")
    # The offending expression must be echoed back so the user knows which
    # filter was rejected, and the error must not be a Python traceback.
    expect(result.stdout + result.stderr).to_contain("project ==")
    expect(result.stdout + result.stderr).not_to_contain("Traceback")


@pytest.mark.release
@pytest.mark.timeout(60)
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
    #
    # We pin discovery to the local provider (an extra flag on top of the
    # tutorial command). The "." -> current-project expansion happens in
    # build_agent_filter_cel *before* any provider is queried, so --provider
    # local exercises the feature faithfully while keeping the listing
    # deterministic: a bare `mngr list` fans out to every enabled backend and,
    # under the default ErrorBehavior.ABORT, exits non-zero if any of them is
    # unreachable (e.g. no Docker daemon, or AWS/cloud plugins installed in the
    # dev monorepo but without credentials) -- conditions that have nothing to
    # do with the "." expansion under test.
    result = e2e.run(
        "mngr list --project . --provider local", comment="list agents for the current project", timeout=30.0
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_list_project_dot_matches_current_project(e2e: E2eSession) -> None:
    # Same PROJECTS tutorial block as test_list_project_dot, but the "happy
    # path": this proves "." expands to the *live current project* (not a no-op
    # or a literal "." match) by seeding an agent that independently derived the
    # same current project at create time and confirming `--project .` matches
    # it -- while a concrete unrelated project name does not.
    e2e.write_tutorial_block("""
        # the literal "." is expanded to the current project (derived from your git worktree
        # root's remote origin, falling back to its source-repo dir name (for worktrees) or
        # folder name, so it stays correct from any subdirectory), so this lists agents for
        # the project you're currently in:
        mngr list --project .
        # this also works for "mngr kanpan --project ."
    """)
    # An agent created without --project inherits the current project (derived
    # from the same git worktree that `mngr list --project .` derives from), so
    # both sides resolve to the identical project name regardless of which
    # fallback (remote origin / dir name) is used. This is what makes the test
    # robust without hardcoding the derived name.
    expect(
        e2e.run(
            "mngr create dot-current-agent --type command --no-ensure-clean --no-connect -- sleep 100911",
            comment="create a local agent that inherits the current project",
        )
    ).to_succeed()

    # "." must resolve to the current project and therefore match the agent that
    # inherited it. (--provider local keeps discovery deterministic; see
    # test_list_project_dot for why.)
    matched = e2e.run(
        "mngr list --project . --provider local",
        comment="'.' expands to the current project, matching the inherited-project agent",
        timeout=30.0,
    )
    expect(matched).to_succeed()
    expect(matched.stdout).to_contain("dot-current-agent")

    # A concrete, unrelated project name must NOT match the agent, proving "."
    # resolved to a specific project rather than acting as a match-everything
    # wildcard.
    unrelated = e2e.run(
        "mngr list --project definitely-not-the-current-project --provider local",
        comment="an unrelated project does not match the current-project agent",
        timeout=30.0,
    )
    expect(unrelated).to_succeed()
    expect(unrelated.stdout).not_to_contain("dot-current-agent")


@pytest.mark.release
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
    # --fields controls which columns render, so all three requested fields must
    # appear as column headers (rather than the default field set).
    expect(result.stdout).to_contain("NAME")
    expect(result.stdout).to_contain("PROJECT")
    expect(result.stdout).to_contain("STATE")
    # the listing must show the agent under its project, i.e. the project field
    # is populated from the agent's project label rather than rendered empty.
    expect(result.stdout).to_contain("my-task")
    expect(result.stdout).to_contain("my-project")
    # the state field must also be populated for the same row -- a command agent
    # that was created but not connected is either still starting (RUNNING) or
    # idle and waiting (WAITING).
    assert "WAITING" in result.stdout or "RUNNING" in result.stdout, (
        f"Expected the state column to show the agent's state, got:\n{result.stdout}"
    )
