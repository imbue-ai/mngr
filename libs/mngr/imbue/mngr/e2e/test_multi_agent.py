"""Tests for multi-agent operations (listing, filtering, bulk destroy)."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
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

    # Exec on each individually to verify it is independently reachable.
    for name in ["agent-a", "agent-b", "agent-c"]:
        exec_result = e2e.run(
            f"mngr exec {name} 'echo {name}'",
            comment=f"Exec on {name}",
        )
        expect(exec_result).to_succeed()
        expect(exec_result.stdout).to_contain(name)

    # Coexistence means each agent has its own isolated workspace, not a shared
    # one. Verify this directly: every agent must report a distinct working
    # directory (its own worktree), so a leaked path from one cannot collide
    # with another.
    work_dirs: dict[str, str] = {}
    for name in ["agent-a", "agent-b", "agent-c"]:
        pwd_result = e2e.run(
            f"mngr exec {name} pwd",
            comment=f"Report working directory of {name}",
        )
        expect(pwd_result).to_succeed()
        # `mngr exec` interleaves the command output with a trailing status line
        # ("Command succeeded on agent ..."); the directory is the first
        # absolute path emitted on stdout.
        paths = [line.strip() for line in pwd_result.stdout.splitlines() if line.strip().startswith("/")]
        assert paths, f"no working directory reported for {name}: {pwd_result.stdout!r}"
        work_dirs[name] = paths[0]
    assert len(set(work_dirs.values())) == 3, f"agents must occupy distinct working directories: {work_dirs}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
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
    # The filter must select on actual state, not just name: every agent the
    # --stopped filter returns must really be in the STOPPED state.
    assert all(a["state"] == "STOPPED" for a in stopped_agents), stopped_agents

    # The --stopped flag is documented as an alias for the CEL filter
    # --include 'state == "STOPPED"'. Verify the alias is faithful: the
    # explicit CEL form must return exactly the same set of agents.
    cel_result = e2e.run(
        "mngr list --include 'state == \"STOPPED\"' --format json",
        comment="List stopped agents via explicit CEL filter (alias for --stopped)",
    )
    expect(cel_result).to_succeed()
    cel_names = {a["name"] for a in json.loads(cel_result.stdout)["agents"]}
    assert cel_names == set(stopped_names), (cel_names, stopped_names)

    # Without --stopped, both agents should appear (the non-stopped one may
    # be RUNNING or WAITING depending on timing)
    all_result = e2e.run(
        "mngr list --format json",
        comment="List all agents (no state filter)",
    )
    expect(all_result).to_succeed()
    all_agents = json.loads(all_result.stdout)["agents"]
    agent_states = {a["name"]: a["state"] for a in all_agents}
    assert "running-agent" in agent_states
    assert "stopped-agent" in agent_states
    # The two agents must end up in distinct states: the one we stopped is
    # STOPPED, while the untouched one is still alive (RUNNING or WAITING).
    assert agent_states["stopped-agent"] == "STOPPED", agent_states
    assert agent_states["running-agent"] in ("RUNNING", "WAITING"), agent_states
