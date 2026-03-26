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
        "mng list --format json",
        comment="the defaults are the following: agent=claude, provider=local, project=current dir",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    agent = matching[0]
    # Default should create a worktree (not in-place)
    assert "worktrees" in agent["work_dir"], f"Expected default create to use a worktree, got: {agent['work_dir']}"


@pytest.mark.release
@pytest.mark.tmux
def test_create_in_place(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # if you want the default behavior of claude (starting in-place), you can specify that:
    mng create --in-place
    # mng defaults to creating a new worktree for each agent because the whole point of mng is to let you run multiple agents in parallel.
    # without creating a new worktree for each, they will make conflicting changes with one another.
    """)
    result = e2e.run(
        "mng create my-task --in-place --command 'sleep 99999' --no-ensure-clean",
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
    # With --in-place, the work directory should be the session cwd (the temp git repo),
    # not a generated worktree path. Use `pwd` to get the resolved cwd for comparison.
    pwd_result = e2e.run("pwd", comment="get resolved cwd for comparison")
    expect(pwd_result).to_succeed()
    assert agent_work_dir == pwd_result.stdout.strip()


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
    assert matching[0]["state"] in ("RUNNING", "WAITING")


@pytest.mark.release
@pytest.mark.tmux
def test_create_codex_agent_not_installed(e2e: E2eSession) -> None:
    """Unhappy path: creating a codex agent when codex is not installed should fail gracefully."""
    e2e.write_tutorial_block("""
    # you can also specify a different agent (ex: codex)
    mng create my-task codex
    """)
    # Do NOT configure a custom command -- rely on the default 'codex' binary which is not installed
    result = e2e.run(
        "mng create my-task codex --no-ensure-clean",
        comment="codex is not installed, so the agent should fail to start",
    )
    # The create command succeeds (agent state is created) but the process exits immediately
    # because the codex binary doesn't exist
    expect(result).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Check agent state after failed start")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    # The agent should be DONE since the codex command exited immediately
    assert matching[0]["state"] == "DONE"
    assert matching[0]["command"] == "codex"


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


@pytest.mark.release
@pytest.mark.tmux
def test_create_named_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mng create my-task
    # that command give the agent a name of "my-task". If you don't specify a name, mng will generate a random one for you.
    """)
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="when creating agents to accomplish tasks, it's recommended that you give them a name",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_named_agent_duplicate_name(e2e: E2eSession) -> None:
    """Creating an agent with a name that already exists should fail."""
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mng create my-task
    # that command give the agent a name of "my-task". If you don't specify a name, mng will generate a random one for you.
    """)
    # Create the first agent
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create the first agent with a given name",
        )
    ).to_succeed()

    # Try to create another agent with the same name
    duplicate_result = e2e.run(
        "mng create my-task --command 'sleep 99999' --no-ensure-clean",
        comment="Attempting to create a second agent with the same name should fail",
    )
    expect(duplicate_result).to_fail()
    expect(duplicate_result.stderr).to_contain("already exists")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_json_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mng create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mng create my-task --no-connect --command 'sleep 99999' --no-ensure-clean --format json",
        comment="you can control output format for scripting",
    )
    expect(create_result).to_succeed()

    # The whole point of --format json is that create produces parseable JSON
    create_json = json.loads(create_result.stdout)
    assert "agent_id" in create_json
    assert "host_id" in create_json

    list_result = e2e.run("mng list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    assert len(agents) == 1
    assert agents[0]["name"] == "my-task"


@pytest.mark.release
@pytest.mark.tmux
def test_create_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # mng is very much meant to be used for scripting and automation, so nothing requires interactivity.
    # if you want to be sure that interactivity is disabled, you can use the --headless flag:
    mng create my-task --headless
    """)
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean --headless",
            comment="if you want to be sure that interactivity is disabled, you can use the --headless flag",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")
