"""Tests for agent lifecycle operations (stop, start, exec, destroy)."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_full_lifecycle(e2e: E2eSession) -> None:
    """Verify the full agent lifecycle: create, exec, stop, start, exec-after-restart, destroy.

    Creates a command agent running `sleep 100100`, then asserts each lifecycle stage
    has its expected observable effect: exec runs commands in the live agent; `stop`
    transitions it to STOPPED in `mngr list`; `start` returns it to RUNNING/WAITING and
    relaunches the agent's own `sleep 100100` command (confirmed via `ps aux`, not merely
    that exec works); and `destroy --force` removes it so `mngr list` reports no agents.
    Each assertion would fail if the corresponding lifecycle operation were a no-op.
    """
    # Create
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100100",
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

    # Verify start actually relaunched the agent's own command, not just that
    # exec works: the "sleep 100100" process must be running again after restart.
    ps_after_restart = e2e.run(
        "mngr exec my-task 'ps aux'", comment="Verify the agent command is running after restart"
    )
    expect(ps_after_restart).to_succeed()
    expect(ps_after_restart.stdout).to_contain("sleep 100100")

    # Destroy
    expect(e2e.run("mngr destroy my-task --force", comment="Destroy the agent")).to_succeed()

    list_after_destroy = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after_destroy).to_succeed()
    expect(list_after_destroy.stdout).to_contain("No agents found")
