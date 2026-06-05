"""Tests for the MANAGING SNAPSHOTS tutorial section.

Each test corresponds 1:1 to a tutorial script block. Snapshots are a
provider-specific feature (only modal supports them in our test matrix), so
each test creates a modal agent first.
"""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_modal_my_task(e2e: E2eSession) -> None:
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean",
            comment="create modal my-task for snapshot test",
            timeout=180.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_snapshot_create(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a snapshot of an agent's host
        mngr snapshot create my-task
    """)
    _create_modal_my_task(e2e)
    expect(e2e.run("mngr snapshot create my-task", comment="create a snapshot of an agent's host")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_snapshot_create_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr snap create my-task
    """)
    _create_modal_my_task(e2e)
    expect(e2e.run("mngr snap create my-task", comment="short form")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_snapshot_create_named(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a snapshot with a descriptive name
        mngr snapshot create my-task --name "before-refactor"
    """)
    _create_modal_my_task(e2e)
    expect(
        e2e.run(
            'mngr snapshot create my-task --name "before-refactor"',
            comment="create a snapshot with a descriptive name",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_snapshot_create_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # snapshot all agents' hosts
        mngr list --ids | mngr snapshot create -
    """)
    _create_modal_my_task(e2e)
    expect(e2e.run("mngr list --ids | mngr snapshot create -", comment="snapshot all agents' hosts")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_snapshot_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all snapshots
        mngr snapshot list
    """)
    expect(e2e.run("mngr snapshot list", comment="list all snapshots")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_list_for_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list snapshots for a specific agent's host
        mngr snapshot list my-task
    """)
    _create_modal_my_task(e2e)
    expect(e2e.run("mngr snapshot list my-task", comment="list snapshots for a specific agent's host")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_list_limit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # limit the number of snapshots shown
        mngr snapshot list my-task --limit 5
    """)
    _create_modal_my_task(e2e)
    expect(e2e.run("mngr snapshot list my-task --limit 5", comment="limit the number of snapshots shown")).to_succeed()


@pytest.mark.release
@pytest.mark.modal
def test_snapshot_destroy_by_id_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy a specific snapshot
        mngr snapshot destroy --snapshot snap-123abc
    """)
    # snap-123abc is fictional; verify mngr parses the flag and exits cleanly
    # with an error rather than crashing.
    result = e2e.run(
        "mngr snapshot destroy --snapshot snap-123abc",
        comment="destroy a specific snapshot",
    )
    assert result.exit_code != 0 or "not found" in (result.stdout + result.stderr).lower()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(240)
def test_snapshot_destroy_all_for_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all snapshots for an agent's host
        mngr snapshot destroy my-task --all-snapshots --force
    """)
    _create_modal_my_task(e2e)
    expect(
        e2e.run(
            "mngr snapshot destroy my-task --all-snapshots --force",
            comment="destroy all snapshots for an agent's host",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(180)
def test_snapshot_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be destroyed
        mngr snapshot destroy my-task --all-snapshots --dry-run
    """)
    _create_modal_my_task(e2e)
    expect(
        e2e.run(
            "mngr snapshot destroy my-task --all-snapshots --dry-run",
            comment="dry-run to see what would be destroyed",
        )
    ).to_succeed()
