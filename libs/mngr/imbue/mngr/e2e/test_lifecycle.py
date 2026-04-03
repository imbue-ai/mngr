"""Tests for agent lifecycle operations (stop, start, exec, destroy)."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_full_lifecycle(e2e: E2eSession) -> None:
    # Create
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --no-connect",
            comment="Create agent for full lifecycle test",
        )
    ).to_succeed()

    # Exec to verify running
    exec_result = e2e.run("mngr exec my-task 'echo alive'", comment="Verify agent is running via exec")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("alive")

    # Stop
    expect(e2e.run("mngr stop my-task", comment="Stop the agent")).to_succeed()

    list_after_stop = e2e.run("mngr list", comment="Verify agent is STOPPED")
    expect(list_after_stop).to_succeed()
    expect(list_after_stop.stdout).to_match(r"my-task\s+STOPPED")

    # Start
    expect(e2e.run("mngr start my-task", comment="Start the agent again")).to_succeed()

    list_after_start = e2e.run("mngr list", comment="Verify agent is RUNNING after restart")
    expect(list_after_start).to_succeed()
    expect(list_after_start.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    # Exec again after restart
    exec_after_restart = e2e.run("mngr exec my-task 'echo still-alive'", comment="Verify exec works after restart")
    expect(exec_after_restart).to_succeed()
    expect(exec_after_restart.stdout).to_contain("still-alive")

    # Destroy
    expect(e2e.run("mngr destroy my-task --force", comment="Destroy the agent")).to_succeed()

    list_after_destroy = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after_destroy).to_succeed()
    expect(list_after_destroy.stdout).to_contain("No agents found")
