"""Basic end-to-end tests for the mng CLI.

These tests exercise mng exclusively through its CLI interface -- no library
imports from mng are used. Each test creates agents with --no-connect to avoid
triggering the tmux attach code path.
"""

import json

import pytest

from imbue.mng.e2e.conftest import MngRunFn
from imbue.mng.utils.testing import get_short_random_string
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
def test_create_and_list_agent(mng: MngRunFn) -> None:
    agent_name = f"e2e-create-{get_short_random_string()}"
    create_result = mng(
        f"create {agent_name} --no-connect --await-ready --agent-cmd 'sleep 847291' --no-ensure-clean",
    )
    expect(create_result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(rf"{agent_name}\s+(RUNNING|WAITING)")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_json_output(mng: MngRunFn) -> None:
    agent_name = f"e2e-json-{get_short_random_string()}"
    result = mng(
        f"create {agent_name} --no-connect --await-ready --agent-cmd 'sleep 934172' --no-ensure-clean --format json",
    )
    expect(result).to_succeed()

    # The JSON output is on the first line; subsequent lines may contain
    # informational messages (e.g. "Shell cwd was reset to ...")
    first_json_line = result.stdout.strip().splitlines()[0]
    parsed = json.loads(first_json_line)
    assert "agent_id" in parsed


@pytest.mark.release
@pytest.mark.tmux
def test_create_headless(mng: MngRunFn) -> None:
    agent_name = f"e2e-headless-{get_short_random_string()}"
    result = mng(
        f"create {agent_name} --no-connect --await-ready --headless --agent-cmd 'sleep 621847' --no-ensure-clean",
    )
    expect(result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_destroy_agent(mng: MngRunFn) -> None:
    agent_name = f"e2e-destroy-{get_short_random_string()}"
    create_result = mng(
        f"create {agent_name} --no-connect --await-ready --agent-cmd 'sleep 537182' --no-ensure-clean",
    )
    expect(create_result).to_succeed()

    destroy_result = mng(f"destroy {agent_name} --force")
    expect(destroy_result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_and_rename_agent(mng: MngRunFn) -> None:
    old_name = f"e2e-rename-old-{get_short_random_string()}"
    new_name = f"e2e-rename-new-{get_short_random_string()}"

    create_result = mng(
        f"create {old_name} --no-connect --await-ready --agent-cmd 'sleep 283746' --no-ensure-clean",
    )
    expect(create_result).to_succeed()

    rename_result = mng(f"rename {old_name} {new_name}")
    expect(rename_result).to_succeed()

    list_result = mng("list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(new_name)
    expect(list_result.stdout).not_to_contain(old_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_label_shows_in_list(mng: MngRunFn) -> None:
    agent_name = f"e2e-label-{get_short_random_string()}"
    create_result = mng(
        f"create {agent_name} --no-connect --await-ready"
        f" --agent-cmd 'sleep 174629'"
        f" --no-ensure-clean"
        f" --label team=backend",
    )
    expect(create_result).to_succeed()

    list_result = mng("list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == agent_name]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
