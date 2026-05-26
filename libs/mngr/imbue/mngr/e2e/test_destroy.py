"""Tests for destroying agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(60)
def test_create_and_destroy_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # destroy without confirmation prompt
    mngr destroy my-task --force
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100098",
            comment="Create agent to be destroyed",
        )
    ).to_succeed()

    # Confirm the agent's sleep process is actually running before destroy, so
    # the post-destroy check below is meaningful (no match could otherwise
    # mean the agent never started). The bracketed `[s]` regex prevents pgrep
    # from matching its own argv ("[s]leep 100098" is not the literal "sleep
    # 100098" string that pgrep's command line contains).
    pre_pgrep = e2e.run(
        "pgrep -f '[s]leep 100098' >/dev/null && echo running || echo missing",
        comment="Confirm the agent process is running before destroy",
    )
    expect(pre_pgrep).to_succeed()
    expect(pre_pgrep.stdout).to_contain("running")

    destroy_result = e2e.run(
        "mngr destroy my-task --force",
        comment="destroy without confirmation prompt",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed agent: my-task")

    list_result = e2e.run("mngr list", comment="Verify agent no longer appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")
    expect(list_result.stdout).to_contain("No agents found")

    # Destroying the agent should also reap its underlying sleep process.
    post_pgrep = e2e.run(
        "pgrep -f '[s]leep 100098' >/dev/null && echo running || echo missing",
        comment="Confirm the agent process was killed by destroy",
    )
    expect(post_pgrep).to_succeed()
    expect(post_pgrep.stdout).to_contain("missing")
