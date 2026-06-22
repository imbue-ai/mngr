"""Tests for agent lifecycle operations (stop, start, exec, destroy)."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(300)
def test_full_lifecycle(e2e: E2eSession) -> None:
    # The agent under test lives on the local provider, so every command is
    # scoped to it. The e2e environment registers credential-based cloud
    # provider plugins (e.g. AWS) that it cannot reach, and an unscoped command
    # fans discovery out to all of them: `mngr list` then aborts with a non-zero
    # exit on the unreachable provider, and targeted commands (start, the
    # post-destroy gc) hang on its credential lookup. Pinning the local provider
    # -- via `--provider local` for list and the `@localhost.local` address for
    # targeted commands -- keeps the test exercising the real lifecycle without
    # depending on remote-provider credentials. `--no-gc` likewise skips the
    # post-destroy garbage-collection sweep, which would otherwise fan out to
    # those same unreachable providers (it cannot reclaim the local host anyway).
    agent = "my-task@localhost.local"

    # Create
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --no-connect -- sleep 100100",
            comment="Create agent for full lifecycle test",
        )
    ).to_succeed()

    # Exec to verify running
    exec_result = e2e.run(f"mngr exec {agent} 'echo alive'", comment="Verify agent is running via exec")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("alive")

    # Stop
    expect(e2e.run(f"mngr stop {agent}", comment="Stop the agent")).to_succeed()

    list_after_stop = e2e.run("mngr list --provider local", comment="Verify agent is STOPPED")
    expect(list_after_stop).to_succeed()
    expect(list_after_stop.stdout).to_match(r"my-task\s+STOPPED")

    # Start
    expect(e2e.run(f"mngr start {agent}", comment="Start the agent again")).to_succeed()

    list_after_start = e2e.run("mngr list --provider local", comment="Verify agent is RUNNING after restart")
    expect(list_after_start).to_succeed()
    expect(list_after_start.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

    # Exec again after restart
    exec_after_restart = e2e.run(f"mngr exec {agent} 'echo still-alive'", comment="Verify exec works after restart")
    expect(exec_after_restart).to_succeed()
    expect(exec_after_restart.stdout).to_contain("still-alive")

    # Verify start actually relaunched the agent's own command, not just that
    # exec works: the "sleep 100100" process must be running again after restart.
    ps_after_restart = e2e.run(f"mngr exec {agent} 'ps aux'", comment="Verify the agent command is running after restart")
    expect(ps_after_restart).to_succeed()
    expect(ps_after_restart.stdout).to_contain("sleep 100100")

    # Destroy
    expect(e2e.run(f"mngr destroy {agent} --force --no-gc", comment="Destroy the agent")).to_succeed()

    list_after_destroy = e2e.run("mngr list --provider local", comment="Verify no agents remain")
    expect(list_after_destroy).to_succeed()
    expect(list_after_destroy.stdout).to_contain("No agents found")
