"""Tests for multi-agent operations (listing, filtering, bulk destroy)."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_multiple_agents_coexist(e2e: E2eSession) -> None:
    for name in ["agent-a", "agent-b", "agent-c"]:
        expect(
            e2e.run(
                f"mngr create {name} --command 'sleep 99999' --no-ensure-clean --no-connect",
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_destroy_all_via_stdin(e2e: E2eSession) -> None:
    for name in ["agent-x", "agent-y"]:
        expect(
            e2e.run(
                f"mngr create {name} --command 'sleep 99999' --no-ensure-clean --no-connect",
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

    list_after = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after).to_succeed()
    expect(list_after.stdout).to_contain("No agents found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_list_filter_by_state(e2e: E2eSession) -> None:
    for name in ["running-agent", "stopped-agent"]:
        expect(
            e2e.run(
                f"mngr create {name} --command 'sleep 99999' --no-ensure-clean --no-connect",
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
