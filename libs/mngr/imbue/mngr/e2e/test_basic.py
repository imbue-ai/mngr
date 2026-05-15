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
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")
    expect(result.stdout).to_contain("destroy")
    expect(result.stdout).to_contain("message")
    expect(result.stdout).to_contain("connect")
    expect(result.stdout).to_contain("push")
    expect(result.stdout).to_contain("pull")
    expect(result.stdout).to_contain("clone")


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
    # Confirm this is the create-specific help (not accidentally top-level help)
    expect(result.stdout).to_contain("Create and run an agent")
    # Confirm the help output is structurally complete (examples section is rendered)
    expect(result.stdout).to_contain("EXAMPLES")


@pytest.mark.release
@pytest.mark.modal
def test_list_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
def test_ls_short_form_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr ls", comment="short form")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_list_json_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr list --format json", comment="List agents as JSON in a fresh environment")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []
    assert parsed["errors"] == []


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
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
@pytest.mark.timeout(120)
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
    assert create_parsed["agent_id"].startswith("agent-")
    assert create_parsed["host_id"].startswith("host-")

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    assert parsed["agents"][0]["id"] == create_parsed["agent_id"]
    assert parsed["agents"][0]["name"] == "my-task"


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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
    expect(destroy_result.stdout).to_match(r"[Dd]estroyed")

    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")
    expect(list_result.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100067",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    # Capture the original agent id so we can verify rename preserves it.
    list_before = e2e.run("mngr list --format json", comment="Capture original agent id before rename")
    expect(list_before).to_succeed()
    before_agents = json.loads(list_before.stdout)["agents"]
    before_matches = [a for a in before_agents if a["name"] == "my-task"]
    assert len(before_matches) == 1, f"Expected exactly one my-task agent before rename, got: {before_agents}"
    original_agent_id = before_matches[0]["id"]

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("Renamed agent: my-task -> renamed-task")

    list_result = e2e.run("mngr list", comment="Verify only the new name appears, with state preserved")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"renamed-task\s+(RUNNING|WAITING)")
    expect(list_result.stdout).not_to_match(r"\bmy-task\b")

    # Renaming must preserve agent identity: the same agent_id should show up
    # under the new name in the JSON listing.
    list_json = e2e.run("mngr list --format json", comment="Verify agent_id is preserved across rename")
    expect(list_json).to_succeed()
    agents = json.loads(list_json.stdout)["agents"]
    matching_agents = [a for a in agents if a["name"] == "renamed-task"]
    assert len(matching_agents) == 1, f"Expected exactly one renamed-task agent, got: {agents}"
    assert matching_agents[0]["id"] == original_agent_id, (
        f"Rename changed agent id: {original_agent_id} -> {matching_agents[0]['id']}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_rename_to_existing_name_fails(e2e: E2eSession) -> None:
    """Renaming an agent to a name that's already taken must fail."""
    expect(
        e2e.run(
            "mngr create first-task --type command --no-ensure-clean -- sleep 100069",
            comment="Create first agent",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create second-task --type command --no-ensure-clean -- sleep 100070",
            comment="Create second agent with a different name",
        )
    ).to_succeed()

    conflict_result = e2e.run(
        "mngr rename second-task first-task",
        comment="Renaming to an existing name should fail",
    )
    expect(conflict_result).to_fail()
    expect(conflict_result.stderr).to_contain("already exists")

    # Both agents must still exist with their original names.
    list_result = e2e.run("mngr list", comment="Verify both original names are still present")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("first-task")
    expect(list_result.stdout).to_contain("second-task")


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
