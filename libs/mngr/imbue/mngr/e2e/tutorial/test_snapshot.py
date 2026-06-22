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
def test_snapshot_create_requires_target(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: `mngr snapshot create` needs at
    # least one agent/host (or '-' for stdin). With no target it must fail
    # cleanly with a usage error rather than crashing, and must not require a
    # provider/host to do so (the validation happens before any discovery).
    e2e.write_tutorial_block("""
        # create a snapshot of an agent's host
        mngr snapshot create my-task
    """)
    result = e2e.run("mngr snapshot create", comment="snapshot create with no target should fail")
    expect(result).to_fail()
    combined_output = result.stdout + result.stderr
    assert "Must specify at least one agent or host" in combined_output, (
        f"expected a usage error about a missing target, got:\n{combined_output}"
    )


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


# No @pytest.mark.modal: this unhappy path fails fast on agent-name resolution
# before any Modal host is created, so the `modal` CLI is never invoked and the
# resource guard would otherwise fail a `modal`-marked test that never called it.
@pytest.mark.release
@pytest.mark.timeout(120)
def test_snapshot_create_short_form_missing_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr snap create my-task
    """)
    # Unhappy path for the same tutorial block: the `snap` short-form alias must
    # go through full command resolution -- including error handling -- and fail
    # cleanly (non-zero exit, clear message) when the named agent does not exist,
    # rather than crashing or silently succeeding.
    result = e2e.run("mngr snap create my-task", comment="short form")
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    assert "Could not find agent" in combined, f"expected a 'Could not find agent' error, got:\n{combined}"
    assert "my-task" in combined, f"expected the missing agent name in the error, got:\n{combined}"


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
    create_result = e2e.run(
        'mngr snapshot create my-task --name "before-refactor"',
        comment="create a snapshot with a descriptive name",
    )
    expect(create_result).to_succeed()
    # Capture the concrete snapshot id so we can tie the name back to the exact
    # snapshot that was just created, rather than trusting a loose substring match.
    id_match = re.search(r"Created snapshot (\S+) for host", create_result.stdout)
    assert id_match is not None, f"expected a snapshot id in create output:\n{create_result.stdout}"
    snapshot_id = id_match.group(1)

    # Verify the --name was actually honored: the snapshot must show up under the
    # given name in the human-readable listing, not just exit cleanly.
    listing = e2e.run("mngr snapshot list my-task", comment="verify the named snapshot exists")
    expect(listing).to_succeed()
    assert "before-refactor" in listing.stdout, f"expected 'before-refactor' snapshot in listing:\n{listing.stdout}"

    # ...and confirm precisely that the freshly-created snapshot id carries that
    # name (the table substring alone could match any column), via the JSON form.
    json_listing = e2e.run(
        "mngr snapshot list my-task --format json",
        comment="confirm the created snapshot carries the given name",
    )
    expect(json_listing).to_succeed()
    snapshots = json.loads(json_listing.stdout)["snapshots"]
    named = {snap["id"]: snap["name"] for snap in snapshots}
    assert named.get(snapshot_id) == "before-refactor", (
        f"expected snapshot {snapshot_id} to be named 'before-refactor', got listing: {named}"
    )


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
    # Capture the exact snapshot id the stdin pipeline produced so we can confirm
    # *this* snapshot persisted, not merely that some snapshot exists (the agent
    # already has an "initial" snapshot from creation).
    id_match = re.search(r"Created snapshot (\S+) for host", create_result.stdout)
    assert id_match is not None, f"Expected a snapshot id in output:\n{create_result.stdout}"
    created_id = id_match.group(1)

    # Verify the snapshot actually persisted by listing it independently. The
    # snapshot metadata lives on the Modal volume, so a fresh process must see it.
    list_result = e2e.run(
        "mngr snapshot list my-task --format json",
        comment="verify the snapshot was recorded for my-task",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert parsed["count"] >= 1, f"Expected at least one snapshot for my-task, got: {parsed}"
    persisted_ids = [snapshot["id"] for snapshot in parsed["snapshots"]]
    assert created_id in persisted_ids, f"Expected {created_id} from the stdin pipeline in listing, got: {parsed}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(240)
def test_snapshot_list(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list snapshots for all running agents
        mngr list --ids | mngr snapshot list -
    """)
    _create_modal_my_task(e2e)
    # `mngr list --ids` emits every running agent's host id and pipes them into
    # `mngr snapshot list -`, which reads the ids from stdin and lists their
    # snapshots. The freshly created modal host has an automatic "initial"
    # snapshot, so it must appear in the combined listing. The pipeline does full
    # provider discovery plus two `mngr` cold starts, so the default 30s command
    # timeout is too tight; give it the same headroom as the create helper.
    result = e2e.run(
        "mngr list --ids | mngr snapshot list -",
        comment="list snapshots for all running agents",
        timeout=180.0,
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("initial")


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

    # Cross-check the human-readable table against the structured listing: the
    # "initial" row must correspond to a real snapshot whose concrete id is also
    # printed in the table. This is what a human would verify interactively --
    # that the row isn't just the literal word "initial" appearing by accident.
    json_result = e2e.run(
        "mngr snapshot list my-task --format json",
        comment="cross-check the listing against structured output",
    )
    expect(json_result).to_succeed()
    parsed = json.loads(json_result.stdout)
    assert parsed["count"] >= 1, f"expected at least one snapshot for my-task, got: {parsed}"
    initial_snapshots = [snap for snap in parsed["snapshots"] if snap["name"] == "initial"]
    assert initial_snapshots, f"expected an 'initial' snapshot in the listing, got: {parsed['snapshots']}"
    # The id of the auto-created "initial" snapshot must appear in the table.
    expect(result.stdout).to_contain(initial_snapshots[0]["id"])


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

    # The tutorial command itself: a generous limit (5) exceeds the number of
    # snapshots, so it must show *all* of them rather than truncating. Both the
    # freshly-created snapshot and the auto-recorded "initial" snapshot appear.
    generous_result = e2e.run("mngr snapshot list my-task --limit 5", comment="limit the number of snapshots shown")
    expect(generous_result).to_succeed()
    expect(generous_result.stdout).to_contain("initial")

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
    # Count exactly how many snapshots exist so we can verify --all-snapshots
    # destroys *all* of them (the host's automatic "initial" plus the one created
    # above, i.e. at least 2).
    count_before = json.loads(
        e2e.run("mngr snapshot list my-task --format json", comment="count snapshots before destroying").stdout
    )["count"]
    assert count_before >= 2, f"expected at least 2 snapshots (initial + created) before destroy, got {count_before}"
    # Destroy every snapshot for the host (the tutorial command under test).
    destroy_result = e2e.run(
        "mngr snapshot destroy my-task --all-snapshots --force",
        comment="destroy all snapshots for an agent's host",
    )
    expect(destroy_result).to_succeed()
    # The command must report destroying *every* pre-existing snapshot, not just
    # some of them -- this is what distinguishes --all-snapshots from a no-op.
    expect(destroy_result.stdout + destroy_result.stderr).to_contain(f"Destroyed {count_before} snapshot(s)")
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
    # ...and the reported count must match the actual number of snapshots, not
    # just any non-empty number.
    count_match = re.search(r"Would destroy (\d+) snapshot", result.stdout)
    assert count_match is not None, f"expected a 'Would destroy N snapshot(s)' line:\n{result.stdout}"
    assert int(count_match.group(1)) == len(ids_before), (
        f"dry-run reported {count_match.group(1)} snapshots but {len(ids_before)} exist"
    )
    for snapshot_id in ids_before:
        expect(result.stdout).to_contain(snapshot_id)

    # ...but must NOT actually destroy anything: every snapshot is still present.
    snapshots_after = json.loads(
        e2e.run("mngr snapshot list my-task --format json", comment="verify nothing was destroyed").stdout
    )["snapshots"]
    ids_after = {snap["id"] for snap in snapshots_after}
    assert ids_after == ids_before, (ids_before, ids_after)
