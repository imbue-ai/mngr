"""Tests for the MANAGING SNAPSHOTS tutorial section.

Each test corresponds 1:1 to a tutorial script block. Snapshots are a
provider-specific feature (only modal supports them in our test matrix), so
each test creates a modal agent first.
"""

import json
import re

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_modal_my_task(e2e: E2eSession) -> None:
    # Use --type command + sleep to avoid the modal claude startup time; the
    # snapshot tests only need a running modal host to snapshot. The test
    # environment has no default agent type configured, so --type is required.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100955",
            comment="create modal my-task for snapshot test",
            timeout=180.0,
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_create(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # create a snapshot of an agent's host
        mngr snapshot create my-task
    """)
    _create_modal_my_task(e2e)
    create_result = e2e.run("mngr snapshot create my-task", comment="create a snapshot of an agent's host")
    expect(create_result).to_succeed()
    # Verify the actual effect: the create output reports a concrete snapshot id
    # (e.g. "Created snapshot <id> for host ..."), and that snapshot must then
    # show up when listing the agent's snapshots.
    id_match = re.search(r"Created snapshot (\S+) for host", create_result.stdout)
    assert id_match is not None, f"Expected a snapshot id in output:\n{create_result.stdout}"
    snapshot_id = id_match.group(1)
    list_result = e2e.run("mngr snapshot list my-task", comment="confirm the snapshot was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(snapshot_id)


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_create_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr snap create my-task
    """)
    _create_modal_my_task(e2e)
    # `snap` is the short-form alias for `snapshot`. Verify it actually creates a
    # snapshot rather than merely exiting cleanly.
    result = e2e.run("mngr snap create my-task", comment="short form")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Created 1 snapshot(s)")
    # Confirm the snapshot persisted by listing the agent's snapshots and checking
    # the freshly-created id appears -- the way a human would verify interactively.
    match = re.search(r"Created snapshot (\S+) for host", result.stdout)
    assert match is not None, f"could not find created snapshot id in output:\n{result.stdout}"
    snapshot_id = match.group(1)
    list_result = e2e.run("mngr snapshot list my-task", comment="verify the snapshot was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(snapshot_id)


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
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
    # Verify the --name was actually honored: the snapshot must show up under the
    # given name in the listing, not just exit cleanly.
    listing = e2e.run("mngr snapshot list my-task", comment="verify the named snapshot exists")
    expect(listing).to_succeed()
    assert "before-refactor" in listing.stdout, f"expected 'before-refactor' snapshot in listing:\n{listing.stdout}"


# Flaky: after the snapshot is recorded on the Modal volume, `set_certified_data`
# does a follow-up direct SSH write of data.json to the host, and Modal's sandbox
# SSH occasionally rejects the (valid) key with a transient "Authentication
# failed" right after the snapshot operation. The snapshot itself always
# succeeds; only this secondary write flakes. offload retries handle it.
@pytest.mark.flaky
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_create_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # snapshot all agents' hosts
        mngr list --ids | mngr snapshot create -
    """)
    _create_modal_my_task(e2e)
    # Run the tutorial command verbatim: list every agent's host id and pipe it
    # into `snapshot create -`, which reads the ids from stdin.
    create_result = e2e.run("mngr list --ids | mngr snapshot create -", comment="snapshot all agents' hosts")
    expect(create_result).to_succeed()
    # The stdin pipeline must have resolved my-task and snapshotted its host.
    expect(create_result.stdout).to_contain("Created snapshot")
    expect(create_result.stdout).to_contain("my-task")

    # Verify the snapshot actually persisted by listing it independently. The
    # snapshot metadata lives on the Modal volume, so a fresh process must see it.
    list_result = e2e.run(
        "mngr snapshot list my-task --format json",
        comment="verify the snapshot was recorded for my-task",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["count"] >= 1, f"Expected at least one snapshot for my-task, got: {parsed}"


@pytest.mark.release
@pytest.mark.modal
def test_snapshot_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list snapshots for all running agents
        mngr list --ids | mngr snapshot list -
    """)
    expect(
        e2e.run("mngr list --ids | mngr snapshot list -", comment="list snapshots for all running agents")
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_snapshot_list_for_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list snapshots for a specific agent's host
        mngr snapshot list my-task
    """)
    _create_modal_my_task(e2e)
    result = e2e.run("mngr snapshot list my-task", comment="list snapshots for a specific agent's host")
    expect(result).to_succeed()
    # Creating a modal host auto-records an "initial" snapshot (the default
    # is_snapshotted_after_create behavior), so the agent-scoped listing must
    # show that snapshot row along with the table header columns.
    expect(result.stdout).to_contain("ID")
    expect(result.stdout).to_contain("NAME")
    expect(result.stdout).to_contain("initial")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_list_limit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # limit the number of snapshots shown
        mngr snapshot list my-task --limit 5
    """)
    _create_modal_my_task(e2e)
    # Creating the modal host already produced an automatic "initial" snapshot;
    # add a second one so that --limit actually has more than one snapshot to
    # truncate (otherwise the flag would be a no-op and the test meaningless).
    expect(e2e.run("mngr snapshot create my-task", comment="create a second snapshot")).to_succeed()

    # The tutorial command itself: a generous limit shows all snapshots.
    expect(e2e.run("mngr snapshot list my-task --limit 5", comment="limit the number of snapshots shown")).to_succeed()

    # Verify --limit truly truncates the output rather than just being accepted.
    # The unlimited list reports every snapshot on the host...
    full_result = e2e.run("mngr snapshot list my-task --format json", comment="list all snapshots for the host")
    expect(full_result).to_succeed()
    full_count = json.loads(full_result.stdout)["count"]
    assert full_count >= 2, f"expected at least 2 snapshots (initial + created), got {full_count}"

    # ...while --limit 1 reports exactly one, fewer than the full list.
    limited_result = e2e.run(
        "mngr snapshot list my-task --limit 1 --format json",
        comment="limit the number of snapshots shown to one",
    )
    expect(limited_result).to_succeed()
    limited_count = json.loads(limited_result.stdout)["count"]
    assert limited_count == 1, f"expected --limit 1 to show exactly 1 snapshot, got {limited_count}"
    assert limited_count < full_count, "expected --limit 1 to truncate the full snapshot list"


@pytest.mark.release
@pytest.mark.modal
def test_snapshot_destroy_by_id_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy a specific snapshot
        mngr snapshot destroy my-task --snapshot snap-123abc
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
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_snapshot_destroy_all_for_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all snapshots for an agent's host
        mngr snapshot destroy my-task --all-snapshots --force
    """)
    _create_modal_my_task(e2e)
    # Create an explicit snapshot so there is at least one concrete snapshot to
    # destroy (the host also gets an automatic "initial" snapshot on create).
    expect(e2e.run("mngr snapshot create my-task", comment="create a snapshot to destroy")).to_succeed()
    # Confirm snapshots exist before destroying them.
    list_before = e2e.run("mngr snapshot list my-task", comment="list snapshots before destroying")
    expect(list_before).to_succeed()
    assert "No snapshots found" not in list_before.stdout + list_before.stderr
    # Destroy every snapshot for the host (the tutorial command under test).
    destroy_result = e2e.run(
        "mngr snapshot destroy my-task --all-snapshots --force",
        comment="destroy all snapshots for an agent's host",
    )
    expect(destroy_result).to_succeed()
    # The command reports how many snapshots it removed.
    assert "Destroyed" in destroy_result.stdout + destroy_result.stderr
    # Listing again confirms every snapshot is actually gone.
    list_after = e2e.run("mngr snapshot list my-task", comment="verify no snapshots remain")
    expect(list_after).to_succeed()
    assert "No snapshots found" in list_after.stdout + list_after.stderr


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_snapshot_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be destroyed
        mngr snapshot destroy my-task --all-snapshots --dry-run
    """)
    _create_modal_my_task(e2e)
    # Create a snapshot so the dry-run has something concrete to report on.
    expect(e2e.run("mngr snapshot create my-task", comment="create a snapshot to preview")).to_succeed()
    snapshots_before = json.loads(
        e2e.run("mngr snapshot list my-task --format json", comment="list snapshots before dry-run").stdout
    )["snapshots"]
    ids_before = {snap["id"] for snap in snapshots_before}
    assert ids_before, "expected at least one snapshot to exist before the dry-run"

    result = e2e.run(
        "mngr snapshot destroy my-task --all-snapshots --dry-run",
        comment="dry-run to see what would be destroyed",
    )
    expect(result).to_succeed()
    # The dry-run must report that it *would* destroy every existing snapshot...
    expect(result.stdout).to_contain("Would destroy")
    for snapshot_id in ids_before:
        expect(result.stdout).to_contain(snapshot_id)

    # ...but must NOT actually destroy anything: every snapshot is still present.
    snapshots_after = json.loads(
        e2e.run("mngr snapshot list my-task --format json", comment="verify nothing was destroyed").stdout
    )["snapshots"]
    ids_after = {snap["id"] for snap in snapshots_after}
    assert ids_after == ids_before, (ids_before, ids_after)
