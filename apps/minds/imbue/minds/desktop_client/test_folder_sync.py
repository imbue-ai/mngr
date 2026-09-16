"""Integration tests for :class:`FolderSyncManager` against a real subprocess.

The unit tests next door cover the settings and the state machine. What is
left, and what only a real process can show, is that the manager spawns the
command it says it does and stops it again. The stand-in for ``mngr`` lives in
``testing.py``: it records the argv it was handed and then blocks until it is
signalled, exactly as a real pairing blocks until Ctrl+C.
"""

import json
import stat
import sys
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync import FolderSyncState
from imbue.minds.desktop_client.folder_sync import _MAX_STEPS_PER_DESTINATION
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncRecord
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore
from imbue.minds.desktop_client.testing import FAKE_WORKSPACE_HOME
from imbue.minds.desktop_client.testing import write_fake_mngr_pair_script
from imbue.minds.errors import FolderSyncError

_AGENT_ID = "agent-000102030405060708090a0b0c0d0e0f"

# Generous: the stand-in has to be spawned and answer before this elapses.
_START_TIMEOUT_SECONDS = 20.0
_DEVICE_ID = "host-0f0e0d0c0b0a09080706050403020100"
# The host id ``StaticBackendResolver`` reports for an agent it knows.
_HOST_ID = "localhost"
_OTHER_AGENT_ID = "agent-0f0e0d0c0b0a090807060504030201ff"


@pytest.fixture
def folder_sync_store(tmp_path: Path) -> FolderSyncStore:
    """The record of what should be synced, over its own directory under ``tmp_path``."""
    return FolderSyncStore(records_dir=tmp_path / "folder_syncs")


@pytest.fixture
def folder_sync_manager(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, folder_sync_store: FolderSyncStore
) -> FolderSyncManager:
    """A manager whose ``mngr`` is the stand-in above, rooted at ``tmp_path``."""
    return FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={_AGENT_ID: {}, _OTHER_AGENT_ID: {}}),
        store=folder_sync_store,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_starting_a_sync_runs_mngr_pair_with_the_settings_the_user_chose(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    local_folder = tmp_path / "notes"
    local_folder.mkdir()

    started = folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.TO_WORKSPACE,
        conflict=FolderSyncConflict.WORKSPACE,
    )
    # The row exists at once, in STARTING; bringing the sync up is asynchronous.
    assert started.state == FolderSyncState.STARTING
    status = folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert status is not None
    assert status.state == FolderSyncState.SYNCED
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[0] == "pair"
    assert argv[argv.index("--source-host") + 1] == _HOST_ID
    assert "--no-require-git" in argv
    assert argv[argv.index("--source-path") + 1] == f"{FAKE_WORKSPACE_HOME}/synced_folders/{_DEVICE_ID}{local_folder}"
    assert argv[argv.index("--target") + 1] == str(local_folder)
    assert argv[argv.index("--sync-direction") + 1] == "reverse"
    assert argv[argv.index("--conflict") + 1] == "source"

    # Turning it off returns at once too: setting the copy aside is another
    # round trip, and a click never waits for one.
    assert folder_sync_manager.stop(_AGENT_ID, str(local_folder)).state == FolderSyncState.DEACTIVATING


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_two_workspaces_may_each_keep_their_own_copy_of_one_folder(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    try:
        # Asking again for the same destination is idempotent -- it is a click
        # on a switch that is already there.
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
        assert len(folder_sync_manager.list_for_agent(_AGENT_ID)) == 1

        # Another workspace is a different sync of the same folder, not a
        # second claim on the first one: the copies land under different
        # machines' homes, and everything here is keyed by the pair.
        folder_sync_manager.start(
            agent_id=_OTHER_AGENT_ID,
            raw_local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
        folder_sync_manager.wait_until_started(_OTHER_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
        assert len(folder_sync_manager.list_for_agent(_AGENT_ID)) == 1
        assert len(folder_sync_manager.list_for_agent(_OTHER_AGENT_ID)) == 1
        # The first workspace's sync is untouched by the second's arrival.
        first = folder_sync_manager.status_for_path(_AGENT_ID, str(local_folder))
        assert first is not None
        assert first.spec.agent_id == _AGENT_ID
    finally:
        folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_stopping_the_last_sync_leaves_nothing_running(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop_all()
    assert all(status.state == FolderSyncState.STOPPED for status in folder_sync_manager.list_for_agent(_AGENT_ID))
    folder_sync_manager.forget(_AGENT_ID, str(local_folder))
    assert folder_sync_manager.list_for_agent(_AGENT_ID) == ()


def test_stopping_a_sync_that_never_started_is_not_an_error(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """The user is entitled to turn something off whatever state the app got into.

    A restore that failed leaves a record saying ACTIVE and nothing running, so
    there is a ticked checkbox with no sync behind it. Refusing there left the
    user with a switch that would not move.
    """
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
            activity=FolderSyncActivity.ACTIVE,
        )
    )

    stopped = folder_sync_manager.stop(_AGENT_ID, str(local_folder))

    assert stopped.state in (FolderSyncState.DEACTIVATING, FolderSyncState.STOPPED)
    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.INACTIVE


def test_stopping_a_folder_nothing_knows_about_is_a_no_op(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """Already off is already off, whether or not anything remembers it."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()

    assert folder_sync_manager.stop(_AGENT_ID, str(local_folder)).state == FolderSyncState.STOPPED


def test_a_restore_that_fails_is_reported_on_the_row_not_only_the_log(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    folder_sync_store: FolderSyncStore,
) -> None:
    """A ticked checkbox with nothing behind it has to say why, or it says nothing at all."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
            activity=FolderSyncActivity.ACTIVE,
        )
    )
    # A resolver that knows no agents is how "Minds cannot say which machine
    # this workspace runs on yet" looks from here.
    manager = FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        store=folder_sync_store,
        # The real wait is two minutes; what this asserts is what happens when
        # it runs out, not how long it is.
        restore_host_wait_seconds=0.2,
    )

    manager.restore_all()

    assert "which machine" in manager.restore_failure_for(_AGENT_ID, str(local_folder))
    # And it is not left looking like a folder the user turned off.
    assert manager.status_for_path(_AGENT_ID, str(local_folder)) is None


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_retrying_brings_a_settled_sync_up_again(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """A sync that has had everything asked of it carried out needs a nudge, not a flip."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop_all()

    folder_sync_manager.retry(_AGENT_ID, str(local_folder))

    status = folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert status is not None
    assert status.state in (FolderSyncState.STARTING, FolderSyncState.SYNCED, FolderSyncState.SYNCING)
    folder_sync_manager.stop_all()


def test_the_reason_a_folder_cannot_sync_is_the_one_starting_it_would_give(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """The pane greys out exactly the folders a click would be refused for.

    Asked as one question rather than two, because a pane that disagreed with
    ``start`` would either refuse something the user could have had or promise
    something that then failed on the click.
    """
    missing = tmp_path / "gone"
    reason = folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(missing))

    assert "nothing at" in reason
    with pytest.raises(FolderSyncError) as refused:
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(missing),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
    assert str(refused.value) == reason


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_folder_inside_a_running_sync_says_so_before_it_is_clicked(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """The overlap refusal reaches the pane too, not only the click."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    assert folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner)) == ""

    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(outer),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)

    assert "one folder is inside the other" in folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner))
    # And the folder that is syncing is not reported as ineligible for it.
    assert folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(outer)) == ""
    folder_sync_manager.stop_all()


def _remember(store: FolderSyncStore, local_path: Path, activity: FolderSyncActivity) -> None:
    """A record of a sync this computer made in some earlier session."""
    store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(local_path),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
            activity=activity,
        )
    )


def test_a_set_aside_copy_blocks_a_folder_inside_it(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """Turning a sync off renames its copy; it does not get out of the way.

    The set-aside tree mirrors the active one, so two overlapping folders nest
    there too. Turning the outer one back on moves its whole directory across,
    carrying the inner one's copy inside the outer's live replica -- and
    removing the outer's copy would delete the inner's with it.
    """
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    _remember(folder_sync_store, outer, FolderSyncActivity.INACTIVE)

    reason = folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner))

    assert "still holds a copy" in reason
    with pytest.raises(FolderSyncError, match="still holds a copy"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(inner),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )


def test_a_removed_copy_does_not_block_a_folder_inside_it(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """DISCARDED is the one state with nothing on disk, so it is out of the way."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    _remember(folder_sync_store, outer, FolderSyncActivity.DISCARDED)

    assert folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner)) == ""


def test_a_copy_remembered_from_an_earlier_session_blocks_before_anything_touches_it(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """restore_all brings back only ACTIVE records, so an off sync is in the store alone."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    _remember(folder_sync_store, outer, FolderSyncActivity.INACTIVE)
    folder_sync_manager.restore_all()

    # Nothing has put it in memory, and it still has to be seen.
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(outer)) is None
    assert "still holds a copy" in folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner))


def test_a_folder_that_does_not_exist_never_reaches_a_subprocess(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    with pytest.raises(FolderSyncError, match="nothing at"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(tmp_path / "missing"),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
    assert not (tmp_path / "argv.json").exists()


def test_a_relative_folder_never_reaches_a_subprocess(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    with pytest.raises(FolderSyncError, match="absolute path"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path="notes",
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
    assert not (tmp_path / "argv.json").exists()


def test_a_pairing_that_cannot_start_settles_the_row_as_failed(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Nobody is waiting on ``start``, so the reason has to land on the row."""
    failing_mngr = tmp_path / "failing-mngr"
    failing_mngr.write_text(
        "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('Error: Could not find agent\\n')\nsys.exit(1)\n"
    )
    failing_mngr.chmod(failing_mngr.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    manager = FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(failing_mngr),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={_AGENT_ID: {}, _OTHER_AGENT_ID: {}}),
    )
    local_folder = tmp_path / "notes"
    local_folder.mkdir()

    manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )

    status = manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert status is not None
    assert status.state == FolderSyncState.FAILED
    assert "Could not find agent" in status.message


def test_a_shared_file_cannot_be_synced(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """unison has no native single-file sync, so Minds does not pretend to."""
    shared_file = tmp_path / "corpus.jsonl"
    shared_file.write_text("{}\n")

    with pytest.raises(FolderSyncError, match="Only folders can be synced"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(shared_file),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
    assert not (tmp_path / "argv.json").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_sync_comes_back_after_a_restart(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """The copy on the machine is meant to be there when Minds is not, so it catches up when Minds is back."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.TO_WORKSPACE,
        conflict=FolderSyncConflict.WORKSPACE,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    # Quitting, which stops every sync without forgetting any of them.
    folder_sync_manager.stop_all()

    restarted = FolderSyncManager(
        concurrency_group=root_concurrency_group,
        mngr_binary=str(write_fake_mngr_pair_script(tmp_path, tmp_path / "argv.json")),
        mngr_host_dir=tmp_path / ".mngr",
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={_AGENT_ID: {}, _OTHER_AGENT_ID: {}}),
        store=FolderSyncStore(records_dir=folder_sync_store.records_dir),
    )
    restarted.restore_all()
    status = restarted.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert status is not None
    assert status.state == FolderSyncState.SYNCED
    # The settings came back with it, not just the fact that something was synced.
    assert status.spec.direction == FolderSyncDirection.TO_WORKSPACE
    assert status.spec.conflict == FolderSyncConflict.WORKSPACE
    restarted.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_sync_turned_off_is_remembered_as_inactive_rather_than_forgotten(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """The machine still holds the copy, so the record is what remembers it is there."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.TO_WORKSPACE,
        conflict=FolderSyncConflict.WORKSPACE,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop(_AGENT_ID, str(local_folder))

    records = folder_sync_store.list_all()
    assert [record.activity for record in records] == [FolderSyncActivity.INACTIVE]
    # The settings survive, so turning it back on resumes rather than restarts.
    assert records[0].direction == FolderSyncDirection.TO_WORKSPACE
    assert records[0].conflict == FolderSyncConflict.WORKSPACE


def test_an_inactive_sync_is_not_restarted_at_launch(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """It is remembered so its copy and settings survive, not so it comes back on its own."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
            activity=FolderSyncActivity.INACTIVE,
        )
    )
    folder_sync_manager.restore_all()
    assert folder_sync_manager.status_for_path(_AGENT_ID, str(local_folder)) is None


def test_discarding_a_copy_is_refused_while_the_sync_is_still_running(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """Its files are in use, and deleting them is the one step that cannot be undone."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    with pytest.raises(FolderSyncError, match="still syncing"):
        folder_sync_manager.discard_copy(_AGENT_ID, str(local_folder))
    folder_sync_manager.stop_all()


def test_discarding_a_copy_records_that_it_is_gone(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """The row has to tell "copy kept" apart from "copy deleted" to know what to offer."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop(_AGENT_ID, str(local_folder))

    folder_sync_manager.discard_copy(_AGENT_ID, str(local_folder))

    records = folder_sync_store.list_all()
    assert [record.activity for record in records] == [FolderSyncActivity.DISCARDED]


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_folder_inside_one_this_workspace_already_syncs_is_refused(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """Two nested copies on one machine destroy each other: see _overlapping_path_for_agent."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(outer),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)

    with pytest.raises(FolderSyncError, match="one folder is inside the other"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(inner),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
    folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_turning_the_outer_sync_off_does_not_free_the_folder_inside_it(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """Off is a rename, not a removal: the copy is still there, still in the way."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(outer),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop(_AGENT_ID, str(outer))
    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)

    with pytest.raises(FolderSyncError, match="still holds a copy"):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(inner),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )

    # Removing the copy is what gets it out of the way.
    folder_sync_manager.discard_copy(_AGENT_ID, str(outer))
    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)
    assert folder_sync_manager.reason_sync_is_unavailable(_AGENT_ID, str(inner)) == ""
    folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_each_workspaces_sync_of_one_folder_is_turned_off_on_its_own(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """The two are separate syncs, so stopping one must leave the other running."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    for agent_id in (_AGENT_ID, _OTHER_AGENT_ID):
        folder_sync_manager.start(
            agent_id=agent_id,
            raw_local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
        folder_sync_manager.wait_until_started(agent_id, str(local_folder), _START_TIMEOUT_SECONDS)

    folder_sync_manager.stop(_AGENT_ID, str(local_folder))

    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.INACTIVE
    assert folder_sync_manager.desired_activity_for(_OTHER_AGENT_ID, str(local_folder)) == FolderSyncActivity.ACTIVE
    still_running = folder_sync_manager.status_for_path(_OTHER_AGENT_ID, str(local_folder))
    assert still_running is not None
    assert still_running.state in (FolderSyncState.STARTING, FolderSyncState.SYNCED, FolderSyncState.SYNCING)
    folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_folder_inside_another_workspaces_sync_is_allowed_and_reported(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """The copies land on different machines, so they cannot collide -- a warning, not a refusal."""
    outer = tmp_path / "work"
    inner = outer / "notes"
    inner.mkdir(parents=True)
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(outer),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(outer), _START_TIMEOUT_SECONDS)

    folder_sync_manager.start(
        agent_id=_OTHER_AGENT_ID,
        raw_local_path=str(inner),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )

    # Each side is told about the other, which is what the pane's warning reads.
    assert folder_sync_manager.overlapping_paths_in_other_workspaces(str(inner), _OTHER_AGENT_ID) == (str(outer),)
    assert folder_sync_manager.overlapping_paths_in_other_workspaces(str(outer), _AGENT_ID) == (str(inner),)
    folder_sync_manager.stop_all()


def test_unsharing_a_path_forgets_it_entirely_and_deletes_its_copy(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """A path nobody shares is not one anybody can sync, so nothing is left to show.

    Nor anything left to delete the copy from, which is why the copy goes too.
    """
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    folder_sync_manager.stop(_AGENT_ID, str(local_folder))

    folder_sync_manager.forget_shared_path(_AGENT_ID, str(local_folder))

    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.DISCARDED
    assert folder_sync_store.list_all() == ()


def test_unsharing_deletes_a_copy_left_over_from_an_earlier_run(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """A sync turned off before Minds last quit is a record and a copy, and nothing in memory.

    Its copy is the one most likely to be forgotten about, so unsharing has to
    reach it from the record alone.
    """
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
            activity=FolderSyncActivity.INACTIVE,
        )
    )

    folder_sync_manager.forget_shared_path(_AGENT_ID, str(local_folder))

    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.DISCARDED
    assert folder_sync_store.list_all() == ()


def test_restoring_a_folder_that_is_gone_leaves_the_others_alone(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """An unmounted disk is one folder's problem, not every folder's."""
    folder_sync_store.remember(
        FolderSyncRecord(
            agent_id=_AGENT_ID,
            local_path=str(tmp_path / "unmounted"),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            device_id=_DEVICE_ID,
        )
    )
    folder_sync_manager.restore_all()
    # It failed on its own row rather than raising, and is still remembered for
    # the next launch: the disk may well be back by then.
    assert folder_sync_manager.status_for_path(_AGENT_ID, str(tmp_path / "unmounted")) is None
    assert len(folder_sync_store.list_all()) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_long_burst_of_clicks_is_not_mistaken_for_a_spin(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """A bored user is entitled to as many moves as they ask for.

    The step limit is per destination, not per worker, so clicking far more
    times than the limit still lands where the last click asked.
    """
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    for _ in range(_MAX_STEPS_PER_DESTINATION * 3):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
        folder_sync_manager.stop(_AGENT_ID, str(local_folder))
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )

    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.ACTIVE
    status = folder_sync_manager.status_for_path(_AGENT_ID, str(local_folder))
    assert status is not None
    assert status.state in (FolderSyncState.STARTING, FolderSyncState.SYNCED, FolderSyncState.SYNCING)
    folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_a_burst_of_clicks_settles_on_the_last_one(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
    folder_sync_store: FolderSyncStore,
) -> None:
    """Clicking back and forth records a destination each time and converges once.

    Not one move per click: the worker re-reads where the folder should end up
    after each step, so the ones the user clicked past are never carried out.
    """
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    for _ in range(3):
        folder_sync_manager.start(
            agent_id=_AGENT_ID,
            raw_local_path=str(local_folder),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
        )
        folder_sync_manager.stop(_AGENT_ID, str(local_folder))
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )

    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)

    # The last click was "on", and that is where it ends up.
    assert folder_sync_manager.desired_activity_for(_AGENT_ID, str(local_folder)) == FolderSyncActivity.ACTIVE
    assert [record.activity for record in folder_sync_store.list_all()] == [FolderSyncActivity.ACTIVE]
    status = folder_sync_manager.status_for_path(_AGENT_ID, str(local_folder))
    assert status is not None
    assert status.state in (FolderSyncState.STARTING, FolderSyncState.SYNCED, FolderSyncState.SYNCING)
    folder_sync_manager.stop_all()


@pytest.mark.skipif(sys.platform == "win32", reason="signal.pause and SIGTERM are POSIX-only")
def test_changing_a_setting_replaces_the_process_without_moving_the_copy(
    tmp_path: Path,
    folder_sync_manager: FolderSyncManager,
) -> None:
    """A flag change should not rename the machine's copy out of the way and back."""
    local_folder = tmp_path / "notes"
    local_folder.mkdir()
    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
    )
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)

    folder_sync_manager.start(
        agent_id=_AGENT_ID,
        raw_local_path=str(local_folder),
        direction=FolderSyncDirection.TO_WORKSPACE,
        conflict=FolderSyncConflict.WORKSPACE,
    )
    assert folder_sync_manager.wait_until_settled(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)
    # Settling means the replacement was spawned; the argv below is written by
    # the process itself, so wait for it to report in before reading it.
    folder_sync_manager.wait_until_started(_AGENT_ID, str(local_folder), _START_TIMEOUT_SECONDS)

    # Still syncing, now on the new settings, and never deactivated on the way.
    status = folder_sync_manager.status_for_path(_AGENT_ID, str(local_folder))
    assert status is not None
    assert status.spec.direction == FolderSyncDirection.TO_WORKSPACE
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert argv[argv.index("--sync-direction") + 1] == "reverse"
    folder_sync_manager.stop_all()
