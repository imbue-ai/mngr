"""Tests for the MANAGING SNAPSHOTS tutorial section.

Each test corresponds 1:1 to a tutorial script block. Snapshots are a
provider-specific feature (only modal supports them in our test matrix), so
each test creates a modal agent first.
"""

import re

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

# Snapshot ids are emitted in the form "im-<ULID>" (e.g. im-01KSTDAKTN8X...).
_SNAPSHOT_ID_PATTERN = r"im-[0-9A-Z]+"


def _create_modal_my_task(e2e: E2eSession) -> None:
    # Use --type command + sleep to give the agent a host to snapshot without
    # paying the modal claude startup cost (and to supply an explicit agent
    # type, since the isolated e2e environment has no default configured).
    expect(
        e2e.run(
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100920",
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100000",
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100130",
            "mngr create my-task --provider modal --type command --no-connect --no-ensure-clean -- sleep 100200",
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
    result = e2e.run("mngr snapshot create my-task", comment="create a snapshot of an agent's host")
    expect(result).to_succeed()
    # Verify the command actually created a snapshot, not just that it exited 0.
    expect(result.stdout).to_contain("Created 1 snapshot(s)")
    id_match = re.search(_SNAPSHOT_ID_PATTERN, result.stdout)
    assert id_match is not None, f"No snapshot id in create output:\n{result.stdout}"
    snapshot_id = id_match.group(0)
    # The created snapshot must now be visible when listing the agent's snapshots.
    listed = e2e.run("mngr snapshot list my-task", comment="verify the snapshot is listed")
    expect(listed).to_succeed()
    expect(listed.stdout).to_contain(snapshot_id)


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
    # The short-form `snap` alias must behave exactly like `snapshot create`.
    create_result = e2e.run("mngr snap create my-task", comment="short form")
    expect(create_result).to_succeed()
    # Verify the alias produced a real snapshot, not just a zero exit code:
    # extract the new snapshot id from the create output and confirm it shows
    # up in `mngr snapshot list` for the agent.
    combined_output = create_result.stdout + create_result.stderr
    snapshot_id_match = re.search(r"Created snapshot (\S+) for host", combined_output)
    assert snapshot_id_match is not None, f"Could not find created snapshot id in output:\n{combined_output}"
    snapshot_id = snapshot_id_match.group(1)
    list_result = e2e.run("mngr snapshot list my-task", comment="verify the new snapshot is listed")
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
    # Verify the snapshot was actually created with the given name, not just
    # that the command exited 0. Listing the agent's snapshots must surface the
    # custom name we passed via --name.
    list_result = e2e.run("mngr snapshot list my-task", comment="confirm the named snapshot exists")
    expect(list_result).to_succeed()
    assert "before-refactor" in list_result.stdout, (
        f"Expected snapshot name 'before-refactor' in snapshot list, got:\n{list_result.stdout}"
    )


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
    result = e2e.run("mngr list --ids | mngr snapshot create -", comment="snapshot all agents' hosts")
    expect(result).to_succeed()
    # The piped command should report creating exactly one snapshot, for my-task's host.
    expect(result.stdout).to_contain("Created 1 snapshot(s)")
    expect(result.stdout).to_contain("my-task")
    # Verify the snapshot is actually persisted and listable for the agent.
    list_result = e2e.run("mngr snapshot list my-task", comment="confirm the snapshot was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"im-[0-9A-Z]+")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list snapshots for all agents' hosts
        mngr list --ids | mngr snapshot list -
    """)
    # `mngr snapshot list` requires at least one agent/host identifier (the
    # `--all` flag was removed); listing across all agents is done by piping
    # their ids in, mirroring `mngr snapshot create -` above. Create a modal
    # agent and a snapshot so the listing has something concrete to return.
    # An explicit `--type command` is supplied because the isolated test
    # environment configures no default agent type.
    expect(
        e2e.run(
            "mngr create my-task --provider modal --no-connect --no-ensure-clean --type command -- sleep 100515",
            comment="create a modal agent to snapshot",
            timeout=180.0,
        )
    ).to_succeed()
    expect(e2e.run("mngr snapshot create my-task", comment="create a snapshot of an agent's host")).to_succeed()
    result = e2e.run("mngr list --ids | mngr snapshot list -", comment="list snapshots for all agents' hosts")
    expect(result).to_succeed()
    # The human table header is only emitted when snapshots exist, so its
    # presence (and the absence of the empty-state message) confirms the
    # snapshot we just created was discovered across all agents' hosts.
    assert "No snapshots found" not in result.stdout, f"Expected snapshots in the listing, got:\n{result.stdout}"
    assert "CREATED" in result.stdout, f"Expected a snapshot table header, got:\n{result.stdout}"


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
    # Creating a modal host makes an "initial" snapshot (is_snapshotted_after_create
    # defaults to True), so listing this agent's host must actually surface that
    # snapshot rather than just exiting cleanly with an empty table.
    expect(result.stdout).to_contain("initial")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_snapshot_list_for_nonexistent_agent(e2e: E2eSession) -> None:
    # Same tutorial block, unhappy path: listing snapshots for an identifier that
    # matches no agent or host must fail rather than silently succeed.
    e2e.write_tutorial_block("""
        # list snapshots for a specific agent's host
        mngr snapshot list my-task
    """)
    result = e2e.run(
        "mngr snapshot list does-not-exist",
        comment="list snapshots for a nonexistent agent",
    )
    expect(result).to_fail()
    expect((result.stdout + result.stderr).lower()).to_contain("not found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
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
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_destroy_all_for_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # destroy all snapshots for an agent's host
        mngr snapshot destroy my-task --all-snapshots --force
    """)
    _create_modal_my_task(e2e)
    # Creating the agent leaves an initial host snapshot behind, so there is at
    # least one snapshot to destroy.
    destroy_result = e2e.run(
        "mngr snapshot destroy my-task --all-snapshots --force",
        comment="destroy all snapshots for an agent's host",
    )
    expect(destroy_result).to_succeed()
    expect(destroy_result.stdout).to_contain("Destroyed")
    # Verify the snapshots are actually gone, not just that the command exited 0.
    list_result = e2e.run("mngr snapshot list my-task", comment="confirm no snapshots remain")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("No snapshots found")


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(180)
def test_snapshot_destroy_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be destroyed
        mngr snapshot destroy my-task --all-snapshots --dry-run
    """)
    _create_modal_my_task(e2e)
    # Modal creates an initial snapshot when the host comes up, so there is at
    # least one snapshot for the dry-run to report on.
    result = e2e.run(
        "mngr snapshot destroy my-task --all-snapshots --dry-run",
        comment="dry-run to see what would be destroyed",
    )
    expect(result).to_succeed()
    # The dry-run must report what *would* be destroyed rather than silently
    # doing nothing.
    expect(result.stdout).to_contain("Would destroy")
    # The defining property of a dry-run: nothing is actually destroyed. The
    # snapshot it just claimed it "would destroy" must still be listed.
    list_result = e2e.run(
        "mngr snapshot list my-task",
        comment="verify the dry-run left the snapshot(s) intact",
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("No snapshots found")
