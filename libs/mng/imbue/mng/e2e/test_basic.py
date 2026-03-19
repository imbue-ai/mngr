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
    result = e2e.run("mng --help")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")


@pytest.mark.release
def test_create_help_succeeds(e2e: Session) -> None:
    result = e2e.run("mng create --help")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--command")


@pytest.mark.release
def test_list_with_no_agents(e2e: Session) -> None:
    result = e2e.run("mng list")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(e2e: Session) -> None:
    result = e2e.run("mng list --format json")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_list_agent(e2e: Session, agent_name: str) -> None:
    expect(e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(rf"{agent_name}\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_json_output(e2e: Session, agent_name: str) -> None:
    expect(e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --format json")).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1


@pytest.mark.release
@pytest.mark.tmux
def test_create_headless(e2e: Session, agent_name: str) -> None:
    expect(e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --headless")).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_destroy_agent(e2e: Session, agent_name: str) -> None:
    expect(e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    destroy_result = e2e.run(f"mng destroy {agent_name} --force")
    expect(destroy_result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(e2e: Session, agent_name: str) -> None:
    expect(e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    new_name = f"e2e-renamed-{agent_name.split('-')[-1]}"
    rename_result = e2e.run(f"mng rename {agent_name} {new_name}")
    expect(rename_result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(new_name)
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label_shows_in_list(e2e: Session, agent_name: str) -> None:
    expect(
        e2e.run(f"mng create {agent_name} --command 'sleep 99999' --no-ensure-clean --label team=backend")
    ).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == agent_name]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
