"""Basic end-to-end tests for the mng CLI.

These tests exercise mng through its CLI interface via subprocess. The e2e
fixture configures a custom connect_command that records tmux sessions via
asciinema instead of attaching interactively.
"""

import json

import pytest

from imbue.skitwright.expect import expect
from imbue.skitwright.session import Session


@pytest.mark.release
def test_help_succeeds(e2e: Session) -> None:
    """
    # or see the other commands--list, destroy, message, connect, push, pull, clone, and more!  These other commands are covered in their own sections below.
    mng --help
    """
    result = e2e.run(
        "mng --help",
        comment="or see the other commands--list, destroy, message, connect, push, pull, clone, and more!",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")


@pytest.mark.release
def test_create_help_succeeds(e2e: Session) -> None:
    """
    # tons more arguments for anything you could want! As always, you can learn more via --help
    mng create --help
    """
    result = e2e.run(
        "mng create --help",
        comment="tons more arguments for anything you could want! As always, you can learn more via --help",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--command")


@pytest.mark.release
def test_list_with_no_agents(e2e: Session) -> None:
    result = e2e.run("mng list", comment="List agents in a fresh environment")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(e2e: Session) -> None:
    result = e2e.run("mng list --format json", comment="List agents as JSON in a fresh environment")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.tmux
def test_create_named_agent(e2e: Session, agent_name: str) -> None:
    """
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mng create my-task
    # that command give the agent a name of "my-task". If you don't specify a name, mng will generate a random one for you.
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean",
            comment="when creating agents to accomplish tasks, it's recommended that you give them a name",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(rf"{agent_name}\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_json_output(e2e: Session, agent_name: str) -> None:
    """
    # you can control output format for scripting:
    mng create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --no-connect --command 'sleep 99999' --no-ensure-clean --format json",
            comment="you can control output format for scripting",
        )
    ).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1


@pytest.mark.release
@pytest.mark.tmux
def test_create_headless(e2e: Session, agent_name: str) -> None:
    """
    # mng is very much meant to be used for scripting and automation, so nothing requires interactivity.
    # if you want to be sure that interactivity is disabled, you can use the --headless flag:
    mng create my-task --headless
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --headless",
            comment="if you want to be sure that interactivity is disabled, you can use the --headless flag",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_destroy_agent(e2e: Session, agent_name: str) -> None:
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    destroy_result = e2e.run(f"mng destroy {agent_name} --force", comment="Destroy the agent")
    expect(destroy_result).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(e2e: Session, agent_name: str) -> None:
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    new_name = f"e2e-renamed-{agent_name.split('-')[-1]}"
    rename_result = e2e.run(
        f"mng rename {agent_name} {new_name}",
        comment=f"Rename agent to {new_name}",
    )
    expect(rename_result).to_succeed()

    list_result = e2e.run("mng list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(new_name)
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label(e2e: Session, agent_name: str) -> None:
    """
    # you can add labels to organize your agents and tags for host metadata:
    mng create my-task --label team=backend --host-label env=staging
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --label team=backend --host-label env=staging",
            comment="you can add labels to organize your agents and tags for host metadata",
        )
    ).to_succeed()

    list_result = e2e.run("mng list --format json", comment="Verify label appears in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == agent_name]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
