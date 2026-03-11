"""Basic end-to-end tests for the mng CLI.

These tests exercise mng through its CLI interface via subprocess. Each test
that creates agents uses --no-connect to avoid triggering the tmux attach
code path.
"""

import json

import pytest

from imbue.mng.e2e.conftest import CreateAgentFn
from imbue.mng.e2e.conftest import MngRunFn
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_help_succeeds(mng: MngRunFn) -> None:
    result = mng("--help")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Usage")
    expect(result.stdout).to_contain("create")
    expect(result.stdout).to_contain("list")


@pytest.mark.release
def test_create_help_succeeds(mng: MngRunFn) -> None:
    result = mng("create --help")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("--no-connect")
    expect(result.stdout).to_contain("--agent-cmd")


@pytest.mark.release
def test_list_with_no_agents(mng: MngRunFn) -> None:
    result = mng("list")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("No agents found")


@pytest.mark.release
def test_list_json_with_no_agents(mng: MngRunFn) -> None:
    result = mng("list --format json")
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert parsed["agents"] == []


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_list_agent(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    agent_name = create_agent("e2e-create")

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(rf"{agent_name}\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_json_output(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    create_agent("e2e-json", extra_args="--format json")

    # Verify the agent appears in the JSON list
    list_result = mng("list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1


@pytest.mark.release
@pytest.mark.tmux
def test_create_headless(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    agent_name = create_agent("e2e-headless", extra_args="--headless")

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_destroy_agent(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    agent_name = create_agent("e2e-destroy")

    destroy_result = mng(f"destroy {agent_name} --force")
    expect(destroy_result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    old_name = create_agent("e2e-rename-old")
    new_name = f"e2e-rename-new-{old_name.split('-')[-1]}"

    rename_result = mng(f"rename {old_name} {new_name}")
    expect(rename_result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(new_name)
    expect(list_result.stdout).not_to_contain(old_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label_shows_in_list(mng: MngRunFn, create_agent: CreateAgentFn) -> None:
    agent_name = create_agent("e2e-label", extra_args="--label team=backend")

    list_result = mng("list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == agent_name]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
