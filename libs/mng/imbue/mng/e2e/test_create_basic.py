"""Tests for basic agent creation from the BASIC CREATION tutorial section."""

import json

import pytest

from imbue.mng.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # running mng create is strictly better than running claude! It's less letters to type :-D
    # running this command launches claude (Claude Code) immediately *in a new worktree*
    mng create
    # the defaults are the following: agent=claude, provider=local, project=current dir
    """)
    result = e2e.run(
        "mng create my-task --command 'sleep 99999' --no-ensure-clean",
        comment="running mng create is strictly better than running claude!",
    )
    expect(result).to_succeed()

    list_result = e2e.run(
        "mng list", comment="the defaults are the following: agent=claude, provider=local, project=current dir"
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_in_place(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # if you want the default behavior of claude (starting in-place), you can specify that:
    mng create --transfer=none
    # mng defaults to creating a new worktree for each agent because the whole point of mng is to let you run multiple agents in parallel.
    # without creating a new worktree for each, they will make conflicting changes with one another.
    """)
    result = e2e.run(
        "mng create my-task --transfer=none --command 'sleep 99999' --no-ensure-clean",
        comment="if you want the default behavior of claude (starting in-place), you can specify that",
    )
    expect(result).to_succeed()

    list_result = e2e.run(
        "mng list --format json",
        comment="mng defaults to creating a new worktree for each agent",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    agent_work_dir = matching[0]["work_dir"]
    # With --transfer=none, the work directory should be the session cwd (the temp git repo),
    # not a generated worktree path.
    assert "worktrees" not in agent_work_dir, f"Expected in-place work_dir to not be a worktree, got: {agent_work_dir}"


@pytest.mark.release
@pytest.mark.tmux
def test_create_short_forms(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use a short form for most commands (like create) as well--the above command is the same as these:
    mng create my-task claude
    mng c my-task
    """)
    # Test "mng create <name>" form (claude is the default type, --command substitutes for the real agent)
    result_full = e2e.run(
        "mng create my-task --command 'sleep 99999' --no-ensure-clean",
        comment="you can use a short form for most commands (like create) as well",
    )
    expect(result_full).to_succeed()

    # Test "mng c <name>" short form (needs a different name since my-task already exists)
    result_short = e2e.run(
        "mng c my-other-task --command 'sleep 99999' --no-ensure-clean",
        comment="the above command is the same as these",
    )
    expect(result_short).to_succeed()

    list_result = e2e.run("mng list", comment="Verify both agents are running")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")
    expect(list_result.stdout).to_match(r"my-other-task\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_codex_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also specify a different agent (ex: codex)
    mng create my-task codex
    """)
    # Configure the codex agent type to use 'sleep 99999' since codex is not installed
    expect(
        e2e.run(
            "mng config set agent_types.codex.command 'sleep 99999'",
            comment="Configure codex command for test environment",
        )
    ).to_succeed()

    result = e2e.run(
        "mng create my-task codex --no-ensure-clean",
        comment="you can also specify a different agent (ex: codex)",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify codex agent is created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["type"] == "codex"


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_agent_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the arguments to the *agent* (ie, send args to claude rather than mng)
    # by using `--` to separate the agent arguments from the mng arguments:
    mng create my-task -- --model opus
    # that command launches claude with the "opus" model instead of the default
    """)
    result = e2e.run(
        "mng create my-task --command 'sleep 99999' --no-ensure-clean -- --model opus",
        comment="you can specify the arguments to the *agent* by using `--` to separate the agent arguments",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify agent args were passed through")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert "--model opus" in matching[0]["command"]
