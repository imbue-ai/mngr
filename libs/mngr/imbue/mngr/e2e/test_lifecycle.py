"""Tests for agent lifecycle operations (stop, start, exec, destroy)."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


# This test drives the lifecycle of a *local* ``command`` agent, so it never
# invokes the Modal CLI binary (the only Modal usage the resource guard can
# observe). The ``mngr list`` calls do attempt Modal *discovery*, but that runs
# as in-process gRPC inside the ``mngr`` subprocess (invisible to the guard) and
# the read-only discovery path never creates an environment, so no ``modal``
# binary is spawned. Adding @pytest.mark.modal would therefore fail the guard's
# NEVER_INVOKED check. Contrast with the Modal *creation* tests, which do carry
# the mark because creating a host invokes ``modal environment create``.
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_full_lifecycle(e2e: E2eSession) -> None:
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

    # Destroy
    expect(e2e.run("mngr destroy my-task --force", comment="Destroy the agent")).to_succeed()

    list_after_destroy = e2e.run("mngr list", comment="Verify no agents remain")
    expect(list_after_destroy).to_succeed()
    expect(list_after_destroy.stdout).to_contain("No agents found")
