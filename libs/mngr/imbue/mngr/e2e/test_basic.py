"""Basic end-to-end tests for the mngr CLI.

These tests exercise mngr through its CLI interface via subprocess. The e2e
fixture configures a custom connect_command that records tmux sessions via
asciinema instead of attaching interactively.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mngr --help
    """)
    result = e2e.run(
        "mngr --help",
        comment="or see the other commands--list, destroy, message, connect, push, pull, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    expect(result.stdout).to_contain("Commands")
    for command in ("create", "list", "destroy", "message", "connect", "push", "pull", "clone"):
        expect(result.stdout).to_contain(command)


@pytest.mark.release
def test_create_help_succeeds(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mngr create --help
    """)
    result = e2e.run(
        "mngr create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--type")


@pytest.mark.release
@pytest.mark.modal
def test_list_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # output all objects as one big JSON array when complete  (useful for scripting)
    mngr list --format json
    """)
    result = e2e.run(
        "mngr list --format json",
        comment="output all objects as one big JSON array when complete  (useful for scripting)",
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_named_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mngr create my-task
    # that command give the agent a name of "my-task". If you don't specify a name, mngr will generate a random one for you.
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100063",
            comment="when creating agents to accomplish tasks, it's recommended that you give them a name",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_with_json_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --format json -- sleep 100064",
        comment="you can control output format for scripting",
    )
    expect(create_result).to_succeed()
    create_parsed = json.loads(create_result.stdout)
    assert "agent_id" in create_parsed
    assert "host_id" in create_parsed

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    assert parsed["agents"][0]["name"] == "my-task"
    assert parsed["agents"][0]["id"] == create_parsed["agent_id"]


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # mngr is very much meant to be used for scripting and automation, so nothing requires interactivity.
    # if you want to be sure that interactivity is disabled, you can use the --headless flag:
    mngr create my-task --headless
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --headless -- sleep 100065",
            comment="if you want to be sure that interactivity is disabled, you can use the --headless flag",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    # --headless skips the post-create connect_command, so no asciinema
    # recording should have been produced (the e2e connect script would
    # write a .cast file otherwise).
    cast_files = list(e2e.output_dir.glob("*.cast"))
    assert cast_files == [], f"Expected no .cast files (headless skips connect), found: {cast_files}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # destroy without confirmation prompt
    mngr destroy my-task --force
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100066",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    destroy_result = e2e.run("mngr destroy my-task --force", comment="destroy without confirmation prompt")
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("my-task")

    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")

    json_list_result = e2e.run("mngr list --format json", comment="Verify JSON list is also empty")
    expect(json_list_result).to_succeed()
    parsed = json.loads(json_list_result.stdout)
    assert parsed["agents"] == []


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --format json -- sleep 100067",
        comment="Create agent to be renamed",
    )
    expect(create_result).to_succeed()
    original_agent_id = json.loads(create_result.stdout)["agent_id"]

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("Renamed agent: my-task -> renamed-task")

    json_list_result = e2e.run("mngr list --format json", comment="Verify rename preserved agent identity")
    expect(json_list_result).to_succeed()
    agents = json.loads(json_list_result.stdout)["agents"]
    # Rename must change only the name; agent identity (id) must be preserved
    # and no new agent should be created.
    assert len(agents) == 1, agents
    assert agents[0]["name"] == "renamed-task"
    assert agents[0]["id"] == original_agent_id

    list_result = e2e.run("mngr list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    # The renamed agent must still be live (RUNNING/WAITING), not a dangling row.
    expect(list_result.stdout).to_match(r"renamed-task\s+(RUNNING|WAITING)")
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --label team=backend --host-label env=staging -- sleep 100068",
            comment="you can add labels to organize your agents and tags for host metadata",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify labels appear in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
    assert matching_agents[0]["host"]["tags"]["env"] == "staging"
