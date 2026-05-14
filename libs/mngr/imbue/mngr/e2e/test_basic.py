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
@pytest.mark.timeout(60)
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
@pytest.mark.timeout(120)
def test_list_with_no_agents(e2e: E2eSession) -> None:
    result = e2e.run("mngr list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(120)
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
        timeout=120.0,
    )
    expect(create_result).to_succeed()
    create_payload = json.loads(create_result.stdout)
    assert set(create_payload) == {"agent_id", "host_id"}, create_payload

    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify agent appears in JSON list",
        timeout=60.0,
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    listed_agent = parsed["agents"][0]
    assert listed_agent["name"] == "my-task"
    assert listed_agent["id"] == create_payload["agent_id"]
    assert listed_agent["host"]["id"] == create_payload["host_id"]


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
            timeout=60.0,
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    exec_result = e2e.run(
        "mngr exec my-task 'ps aux | grep sleep'",
        comment="Verify the agent's sleep command is actually running inside the agent",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("sleep 100065")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100066",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    destroy_result = e2e.run("mngr destroy my-task --force", comment="Destroy the agent")
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")

    list_result = e2e.run("mngr list --format json", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    assert json.loads(list_result.stdout)["agents"] == []


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

    pre_list = e2e.run("mngr list --format json", comment="Capture the agent's id before renaming")
    expect(pre_list).to_succeed()
    pre_agents = json.loads(pre_list.stdout)["agents"]
    pre_match = [a for a in pre_agents if a["name"] == "my-task"]
    assert len(pre_match) == 1, f"Expected exactly one agent named 'my-task', got {pre_agents}"
    original_agent_id = pre_match[0]["id"]

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()
    expect(rename_result.stdout).to_contain("Renamed agent")

    list_result = e2e.run("mngr list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("renamed-task")
    expect(list_result.stdout).not_to_contain("my-task")

    post_list = e2e.run("mngr list --format json", comment="Verify the renamed agent kept its id")
    expect(post_list).to_succeed()
    post_agents = json.loads(post_list.stdout)["agents"]
    post_match = [a for a in post_agents if a["name"] == "renamed-task"]
    assert len(post_match) == 1, f"Expected exactly one agent named 'renamed-task', got {post_agents}"
    assert post_match[0]["id"] == original_agent_id, (
        f"Agent id changed across rename: {original_agent_id} -> {post_match[0]['id']}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
