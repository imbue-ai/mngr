"""Basic end-to-end tests for the mng CLI.

These tests exercise mng through its CLI interface via subprocess. The e2e
fixture configures a custom connect_command that records tmux sessions via
asciinema instead of attaching interactively.
"""

import json

import pytest

from imbue.mng.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mng --help
    """)
    result = e2e.run(
        "mng --help",
        comment="or see the other commands--list, destroy, message, connect, push, pull, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")


@pytest.mark.release
def test_create_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mng create --help
    """)
    result = e2e.run(
        "mng create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--command")


@pytest.mark.release
def test_list_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mng list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mng list --format json", comment="List agents as JSON in a fresh environment")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


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
def test_create_with_json_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mng create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    expect(
        e2e.run(
            "mng create my-task --no-connect --command 'sleep 99999' --no-ensure-clean --format json",
            comment="you can control output format for scripting",
        )
    ).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1


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
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when you're done with an agent, destroy it to clean up all of its resources:
    mng destroy my-task --force
    # use --force to skip confirmation and allow destroying running agents
    """)
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    # Verify agent exists before destroying
    list_result = e2e.run("mng list", comment="Verify agent exists before destroy")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    destroy_result = e2e.run(
        "mng destroy my-task --force",
        comment="when you're done with an agent, destroy it to clean up all of its resources",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")
    expect(destroy_result.stdout).to_contain("Successfully destroyed 1 agent(s)")

    # Verify agent is gone via JSON list (more thorough than text matching)
    list_result = e2e.run("mng list --format json", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.tmux
def test_destroy_with_dry_run(e2e: E2eSession) -> None:
    """Verify --dry-run shows what would be destroyed without actually destroying."""
    e2e.write_tutorial_block("""
    # preview what would be destroyed without actually destroying:
    mng destroy my-task --dry-run
    """)
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent for dry-run test",
        )
    ).to_succeed()

    dry_run_result = e2e.run(
        "mng destroy my-task --dry-run",
        comment="preview what would be destroyed without actually destroying",
    )
    expect(dry_run_result).to_succeed()
    expect(dry_run_result.stdout).to_contain("my-task")

    # Agent should still exist after dry-run
    list_result = e2e.run("mng list", comment="Verify agent still exists after dry-run")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_destroy_all_agents(e2e: E2eSession) -> None:
    """Verify --all --force destroys all agents at once."""
    e2e.write_tutorial_block("""
    # destroy all agents at once:
    mng destroy --all --force
    """)
    expect(
        e2e.run(
            "mng create task-one --command 'sleep 99999' --no-ensure-clean",
            comment="Create first agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mng create task-two --command 'sleep 99999' --no-ensure-clean",
            comment="Create second agent",
        )
    ).to_succeed()

    destroy_result = e2e.run(
        "mng destroy --all --force",
        comment="destroy all agents at once",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Successfully destroyed 2 agent(s)")

    list_result = e2e.run("mng list --format json", comment="Verify all agents are gone")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.tmux
def test_destroy_with_json_output(e2e: E2eSession) -> None:
    """Verify --format json returns structured destroy result."""
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent for JSON destroy test",
        )
    ).to_succeed()

    destroy_result = e2e.run(
        "mng destroy my-task --force --format json",
        comment="Destroy agent with JSON output",
    )
    expect(destroy_result).to_succeed()
    parsed = json.loads(destroy_result.stdout)
    assert parsed["destroyed_agents"] == ["my-task"]
    assert parsed["count"] == 1


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    rename_result = e2e.run(
        "mng rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()

    list_result = e2e.run("mng list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("renamed-task")
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mng create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mng create my-task --command 'sleep 99999' --no-ensure-clean --label team=backend --host-label env=staging",
            comment="you can add labels to organize your agents and tags for host metadata",
        )
    ).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify label appears in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
