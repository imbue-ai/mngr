"""Tests for error handling in the mngr CLI."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_invalid_provider_fails(e2e: E2eSession) -> None:
    result = e2e.run(
        "mngr create my-task --provider nonexistent --no-connect --no-ensure-clean",
        comment="Attempt to create with an invalid provider",
    )
    expect(result).to_fail()


@pytest.mark.release
@pytest.mark.tmux
def test_create_duplicate_name_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --no-connect",
            comment="Create first agent",
        )
    ).to_succeed()

    duplicate_result = e2e.run(
        "mngr create my-task --command 'sleep 99999' --no-ensure-clean --no-connect",
        comment="Attempt to create agent with duplicate name",
    )
    expect(duplicate_result).to_fail()


@pytest.mark.release
def test_create_with_dirty_tree_fails(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "echo 'dirty' > dirty.txt && git add dirty.txt",
            comment="Create a dirty git tree",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr create my-task",
        comment="Attempt to create without --no-ensure-clean in a dirty tree",
    )
    expect(result).to_fail()
