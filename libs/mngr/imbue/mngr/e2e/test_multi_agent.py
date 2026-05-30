"""Tests for multi-agent operations (listing, filtering, bulk destroy)."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# Creating three agents plus a list and three execs exceeds the global 10s
# default timeout, so override it (cf. test_destroy.py's 60s for a single
# create+destroy).
@pytest.mark.timeout(180)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_multiple_agents_coexist(e2e: E2eSession) -> None:
    # Pin a unique sleep value per agent so leaked processes trace back to the specific create call.
    for name, sleep_seconds in [("agent-a", 100101), ("agent-b", 100118), ("agent-c", 100119)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"Create {name}",
            )
        ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify all three agents appear")
    expect(list_result).to_succeed()
    for name in ["agent-a", "agent-b", "agent-c"]:
        expect(list_result.stdout).to_match(rf"{name}\s+(RUNNING|WAITING)")

    # Exec on each individually to verify isolation
    for name in ["agent-a", "agent-b", "agent-c"]:
        exec_result = e2e.run(
            f"mngr exec {name} 'echo {name}'",
            comment=f"Exec on {name}",
        )
        expect(exec_result).to_succeed()
        expect(exec_result.stdout).to_contain(name)

    # The point of running multiple agents is isolation: each must live in its
    # own distinct worktree so their changes never collide. Verify that here.
    json_result = e2e.run("mngr list --format json", comment="Inspect per-agent work directories")
    expect(json_result).to_succeed()
    agents_by_name = {a["name"]: a for a in json.loads(json_result.stdout)["agents"]}
    work_dirs = [agents_by_name[name]["work_dir"] for name in ["agent-a", "agent-b", "agent-c"]]
    for name, work_dir in zip(["agent-a", "agent-b", "agent-c"], work_dirs):
        assert "worktrees" in work_dir, f"Expected {name} to run in a worktree, got: {work_dir}"
    assert len(set(work_dirs)) == 3, f"Expected three distinct work_dirs, got: {work_dirs}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_destroy_all_via_stdin(e2e: E2eSession) -> None:
    # Pin a unique sleep value per agent so leaked processes trace back to the specific create call.
    for name, sleep_seconds in [("agent-x", 100102), ("agent-y", 100120)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"Create {name}",
            )
        ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify both agents exist")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("agent-x")
    expect(list_result.stdout).to_contain("agent-y")

    # Destroy all by piping ids through stdin
    destroy_result = e2e.run(
        "mngr list --ids | mngr destroy - --force",
        comment="Destroy all agents via stdin piping",
    )
    expect(destroy_result).to_succeed()
    # The piped destroy should report both specific agents as destroyed,
    # not just leave an empty list behind.
    expect(destroy_result.stdout).to_contain("agent-x")
    expect(destroy_result.stdout).to_contain("agent-y")
    expect(destroy_result.stdout).to_contain("Successfully destroyed 2 agent(s)")

    list_after = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("No agents found")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_list_filter_by_state(e2e: E2eSession) -> None:
    # Pin a unique sleep value per agent so leaked processes trace back to the specific create call.
    for name, sleep_seconds in [("running-agent", 100103), ("stopped-agent", 100121)]:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_seconds}",
                comment=f"Create {name}",
            )
        ).to_succeed()

    # Stop one agent
    expect(e2e.run("mngr stop stopped-agent", comment="Stop one agent")).to_succeed()

    # --stopped should show only the stopped agent
    stopped_result = e2e.run(
        "mngr list --stopped --format json",
        comment="List only stopped agents",
    )
    expect(stopped_result).to_succeed()
    stopped_agents = json.loads(stopped_result.stdout)["agents"]
    stopped_names = [a["name"] for a in stopped_agents]
    assert "stopped-agent" in stopped_names
    assert "running-agent" not in stopped_names

    # Without --stopped, both agents should appear (the non-stopped one may
    # be RUNNING or WAITING depending on timing)
    all_result = e2e.run(
        "mngr list --format json",
        comment="List all agents (no state filter)",
    )
    expect(all_result).to_succeed()
    all_agents = json.loads(all_result.stdout)["agents"]
    all_names = [a["name"] for a in all_agents]
    assert "running-agent" in all_names
    assert "stopped-agent" in all_names
