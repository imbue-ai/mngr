"""Tests for renaming agents.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_and_rename_agent(e2e: E2eSession) -> None:
    sleep_agent_type = e2e.make_sleep_agent_type()
    expect(
        e2e.run(
            f"mngr create my-task --type {sleep_agent_type} --no-ensure-clean",
            comment="Create agent to be renamed",
        )
    ).to_succeed()

    rename_result = e2e.run(
        "mngr rename my-task renamed-task",
        comment="Rename agent to renamed-task",
    )
    expect(rename_result).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify only the new name appears")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("renamed-task")
    expect(list_result.stdout).not_to_contain("my-task")
