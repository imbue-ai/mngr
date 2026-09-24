"""Unit tests for the folder-sync settings, argv, and state machine."""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.folder_sync import FolderSyncSpec
from imbue.minds.desktop_client.folder_sync import FolderSyncState
from imbue.minds.desktop_client.folder_sync import WORKSPACE_SYNC_DIRECTORY
from imbue.minds.desktop_client.folder_sync import _PairEventSink
from imbue.minds.desktop_client.folder_sync import _WorkspaceTarget
from imbue.minds.desktop_client.folder_sync import _build_folder_sync_spec
from imbue.minds.desktop_client.folder_sync import _build_pair_argv
from imbue.minds.desktop_client.folder_sync import _build_workspace_deactivate_argv
from imbue.minds.desktop_client.folder_sync import _build_workspace_discard_argv
from imbue.minds.desktop_client.folder_sync import _build_workspace_prepare_argv
from imbue.minds.desktop_client.folder_sync import _overlaps
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.errors import FolderSyncError

_AGENT_ID = "agent-000102030405060708090a0b0c0d0e0f"
_DEVICE_ID = "host-0f0e0d0c0b0a09080706050403020100"
_HOST_ID = "host-92ed9fcbce9a4b0fb1340cf39ef15799"
_TARGET = _WorkspaceTarget(path="/home/agent/synced_folders/host-dev/home/someone/notes", is_resumed=True)
# The same directory, but one Minds had to create -- no shared history to read.
_FRESH_TARGET = _WorkspaceTarget(path=_TARGET.path, is_resumed=False)


def _spec(
    direction: FolderSyncDirection = FolderSyncDirection.BOTH,
    conflict: FolderSyncConflict = FolderSyncConflict.NEWER,
    workspace_path: str = "notes",
) -> FolderSyncSpec:
    return FolderSyncSpec(
        agent_id=_AGENT_ID,
        host_id=_HOST_ID,
        local_path="/home/someone/notes",
        workspace_path=workspace_path,
        direction=direction,
        conflict=conflict,
    )


def test_pair_argv_leaves_symlinks_where_they_are() -> None:
    """The two sides are different computers, so a link does not mean the same on both."""
    argv = _build_pair_argv("mngr", _spec(), _TARGET)
    assert "--no-links" in argv


def test_pair_argv_leaves_a_repository_history_behind() -> None:
    """``.git`` is asked for by name, not inherited from a flag that means something else."""
    argv = _build_pair_argv("mngr", _spec(), _TARGET)
    assert argv[argv.index("--exclude") + 1] == ".git"


def test_pair_argv_never_lets_pairing_touch_git() -> None:
    argv = _build_pair_argv("mngr", _spec(), _TARGET)
    assert "--no-require-git" in argv
    assert "--require-git" not in argv
    # The git-sync-only flag has no business being passed at all.
    assert "--uncommitted-changes" not in argv


def test_pair_argv_names_the_machine_and_both_paths() -> None:
    """Addressed by host: what a sync needs is the machine, not an agent on it."""
    argv = _build_pair_argv("/opt/bin/mngr", _spec(), _TARGET)
    assert argv[:2] == ["/opt/bin/mngr", "pair"]
    assert argv[argv.index("--source-host") + 1] == _HOST_ID
    assert _AGENT_ID not in argv
    assert argv[argv.index("--source-path") + 1] == _TARGET.path
    assert argv[argv.index("--target") + 1] == "/home/someone/notes"
    assert argv[argv.index("--format") + 1] == "jsonl"


def test_pair_argv_never_starts_a_stopped_machine() -> None:
    """Restoring syncs is Minds' own doing, and must not be what bills for a machine."""
    assert "--no-start" in _build_pair_argv("mngr", _spec(), _TARGET)
    assert "--start" not in _build_pair_argv("mngr", _spec(), _TARGET)


def test_workspace_prepare_never_starts_a_stopped_machine() -> None:
    assert "--no-start" in _build_workspace_prepare_argv("mngr", _AGENT_ID, _spec())


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (FolderSyncDirection.BOTH, "both"),
        # mngr pair's source is the workspace and its target is this computer,
        # so "to the workspace" is its reverse and not its forward.
        (FolderSyncDirection.TO_WORKSPACE, "reverse"),
    ],
)
def test_direction_maps_onto_the_right_mngr_pair_value(direction: FolderSyncDirection, expected: str) -> None:
    argv = _build_pair_argv("mngr", _spec(direction=direction), _TARGET)
    assert argv[argv.index("--sync-direction") + 1] == expected


@pytest.mark.parametrize(
    ("conflict", "expected"),
    [
        (FolderSyncConflict.NEWER, "newer"),
        (FolderSyncConflict.THIS_COMPUTER, "target"),
        (FolderSyncConflict.WORKSPACE, "source"),
    ],
)
def test_conflict_maps_onto_the_right_mngr_pair_value(conflict: FolderSyncConflict, expected: str) -> None:
    argv = _build_pair_argv("mngr", _spec(conflict=conflict), _TARGET)
    assert argv[argv.index("--conflict") + 1] == expected


def test_the_workspace_side_is_this_device_then_the_whole_local_path(tmp_path: Path) -> None:
    """Not a choice: a shared /home/me/notes from device D lands at ~/synced_folders/D/home/me/notes."""
    shared = tmp_path / "notes"
    shared.mkdir()
    spec = _build_folder_sync_spec(
        agent_id=_AGENT_ID,
        host_id=_HOST_ID,
        raw_local_path=str(shared),
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
    )
    assert spec.workspace_path == f"{_DEVICE_ID}/{str(shared).lstrip('/')}"


def test_two_folders_sharing_a_name_do_not_collide_on_the_machine(tmp_path: Path) -> None:
    """The whole local path is mirrored, so ~/a/notes and ~/b/notes stay apart."""
    first, second = tmp_path / "a" / "notes", tmp_path / "b" / "notes"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    specs = [
        _build_folder_sync_spec(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            raw_local_path=str(path),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            home_dir=tmp_path,
            device_id=_DEVICE_ID,
        )
        for path in (first, second)
    ]
    assert specs[0].workspace_path != specs[1].workspace_path


def test_the_same_path_on_two_computers_does_not_collide(tmp_path: Path) -> None:
    """One workspace can be synced with from several desktops, where /Users/me/notes means different directories."""
    shared = tmp_path / "notes"
    shared.mkdir()
    specs = [
        _build_folder_sync_spec(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            raw_local_path=str(shared),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            home_dir=tmp_path,
            device_id=device_id,
        )
        for device_id in (_DEVICE_ID, "host-ffffffffffffffffffffffffffffffff")
    ]
    assert specs[0].local_path == specs[1].local_path
    assert specs[0].workspace_path != specs[1].workspace_path


@pytest.mark.parametrize(
    "candidate,other,is_overlapping",
    (
        ("/a/work", "/a/work", True),
        ("/a/work/notes", "/a/work", True),
        ("/a/work", "/a/work/notes", True),
        ("/a/work", "/a/workshop", False),
        ("/a/work", "/a/other", False),
        ("/a/work/", "/a/work/notes", True),
        # The usual macOS volume is case-insensitive, so these name one folder.
        ("/a/Work/notes", "/a/work", True),
        ("/a/WORK", "/a/work", True),
        ("/a/Workshop", "/a/work", False),
    ),
)
def test_paths_overlap_when_one_is_the_other_or_inside_it(candidate: str, other: str, is_overlapping: bool) -> None:
    """A sibling whose name merely starts the same way is not inside anything."""
    assert _overlaps(candidate, other) is is_overlapping


def test_a_shared_file_is_refused(tmp_path: Path) -> None:
    """unison has no native single-file sync, so Minds does not offer one."""
    shared = tmp_path / "corpus.jsonl"
    shared.write_text("{}\n")
    with pytest.raises(FolderSyncError, match="Only folders can be synced"):
        _build_folder_sync_spec(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            raw_local_path=str(shared),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            home_dir=tmp_path,
            device_id=_DEVICE_ID,
        )


def test_spec_expands_a_leading_tilde_against_the_home_directory(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes").mkdir(exist_ok=True)
    spec = _build_folder_sync_spec(
        agent_id=_AGENT_ID,
        host_id=_HOST_ID,
        raw_local_path="~/notes",
        direction=FolderSyncDirection.BOTH,
        conflict=FolderSyncConflict.NEWER,
        home_dir=tmp_path,
        device_id=_DEVICE_ID,
    )
    assert spec.local_path == str(tmp_path / "notes")


def test_spec_refuses_a_path_that_is_not_absolute(tmp_path: Path) -> None:
    """The workspace side is built by splicing this, so a relative path has nowhere to land.

    Not a perimeter: a sync is not stopped from reaching outside the folders a
    share may use, because the check that would have said so defends nothing --
    see :func:`_build_folder_sync_spec`.
    """
    with pytest.raises(FolderSyncError, match="absolute path"):
        _build_folder_sync_spec(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            raw_local_path="notes",
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            home_dir=tmp_path,
            device_id=_DEVICE_ID,
        )


def test_spec_refuses_a_path_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(FolderSyncError, match="nothing at"):
        _build_folder_sync_spec(
            agent_id=_AGENT_ID,
            host_id=_HOST_ID,
            raw_local_path=str(tmp_path / "gone"),
            direction=FolderSyncDirection.BOTH,
            conflict=FolderSyncConflict.NEWER,
            home_dir=tmp_path,
            device_id=_DEVICE_ID,
        )


def test_sink_settles_at_synced_once_unison_is_up() -> None:
    """Up and watching is "synced", not "syncing" -- nothing is moving yet."""
    sink = _PairEventSink()
    sink.on_output(json.dumps({"event": "pair_started", "source_path": "/a", "target_path": "/b"}), True)
    assert sink.state == FolderSyncState.STARTING
    sink.on_output(json.dumps({"event": "pair_syncing"}), True)
    assert sink.state == FolderSyncState.SYNCED


def test_sink_settles_a_restart_the_same_way_it_settles_a_first_start() -> None:
    """A restart waits on the same event; only what the row says while it waits differs."""
    sink = _PairEventSink(state=FolderSyncState.RESTARTING)
    sink.on_output(json.dumps({"event": "pair_syncing"}), True)
    assert sink.state == FolderSyncState.SYNCED


def test_sink_shows_syncing_only_while_bytes_are_moving() -> None:
    sink = _PairEventSink()
    sink.on_output(json.dumps({"event": "pair_syncing"}), True)

    sink.on_output(json.dumps({"event": "pair_transferring", "is_transferring": True}), True)
    assert sink.state == FolderSyncState.SYNCING

    sink.on_output(json.dumps({"event": "pair_transferring", "is_transferring": False}), True)
    assert sink.state == FolderSyncState.SYNCED


def test_a_transfer_event_never_revives_a_sync_that_has_ended() -> None:
    """A late event from a dying process must not undo STOPPED or FAILED."""
    sink = _PairEventSink()
    sink.on_output(json.dumps({"event": "pair_syncing"}), True)
    sink.on_exit(1)
    assert sink.state == FolderSyncState.FAILED

    sink.on_output(json.dumps({"event": "pair_transferring", "is_transferring": True}), True)
    assert sink.state == FolderSyncState.FAILED


def test_sink_ignores_lines_that_are_not_the_syncing_event() -> None:
    sink = _PairEventSink()
    sink.on_output("INFO | resolving unison on the host", False)
    sink.on_output("{not json at all", True)
    sink.on_output(json.dumps({"event": "info", "message": "Pairing with agent: alpha"}), True)
    assert sink.state == FolderSyncState.STARTING


def test_sink_settles_a_user_requested_stop_as_stopped_however_the_process_exited() -> None:
    sink = _PairEventSink()
    sink.on_output(json.dumps({"event": "pair_syncing"}), True)
    sink.is_stop_requested = True
    sink.on_exit(143)
    assert sink.state == FolderSyncState.STOPPED
    assert sink.message == ""


def test_sink_settles_an_unasked_for_nonzero_exit_as_a_failure_quoting_the_output() -> None:
    sink = _PairEventSink()
    sink.on_output("Error: unison 2.51 is too old to pair with", False)
    sink.on_exit(1)
    assert sink.state == FolderSyncState.FAILED
    assert "unison 2.51 is too old" in sink.message


def test_sink_keeps_only_a_bounded_tail_of_a_stream_that_never_ends() -> None:
    sink = _PairEventSink()
    for index in range(100):
        sink.on_output(f"line {index}", True)
    assert len(sink.recent_output) <= 8
    assert sink.recent_output[-1] == "line 99"


def test_the_workspace_folder_is_made_under_the_agents_home() -> None:
    """Under the home, not the working directory: that one is a git checkout."""
    argv = _build_workspace_prepare_argv("mngr", _AGENT_ID, _spec())
    assert argv[:3] == ["mngr", "exec", _AGENT_ID]
    assert 'mkdir -p "$HOME"/synced_folders/notes' in argv[3]
    assert 'printf %s "$HOME"' in argv[3]


def test_turning_a_sync_on_brings_back_a_copy_that_was_set_aside() -> None:
    """Resuming a sync should keep the files it had, not re-fetch them."""
    script = _build_workspace_prepare_argv("mngr", _AGENT_ID, _spec())[3]
    assert 'mv "$HOME"/inactive_synced_folders/notes "$HOME"/synced_folders/notes' in script
    # Only when there is something to move and nothing already in its place.
    assert '[ -e "$HOME"/inactive_synced_folders/notes ]' in script
    assert '[ ! -e "$HOME"/synced_folders/notes ]' in script


def test_turning_a_sync_off_sets_the_copy_aside_rather_than_deleting_it() -> None:
    """ "Stop syncing this" and "throw the copy away" are different intentions."""
    script = _build_workspace_deactivate_argv("mngr", _AGENT_ID, _spec())[3]
    assert 'mv "$HOME"/synced_folders/notes "$HOME"/inactive_synced_folders/notes' in script
    # The copy being set aside is never deleted; only its destination is cleared.
    assert 'rm -rf "$HOME"/synced_folders/notes' not in script


def test_setting_a_copy_aside_clears_whatever_is_in_its_way() -> None:
    """Nothing but Minds writes there, so something in the way is a mistake, not a file to keep."""
    script = _build_workspace_deactivate_argv("mngr", _AGENT_ID, _spec())[3]
    assert 'rm -rf "$HOME"/inactive_synced_folders/notes' in script
    assert script.index('rm -rf "$HOME"/inactive_synced_folders/notes') < script.index("  mv ")


def test_discarding_only_ever_deletes_the_set_aside_copy() -> None:
    """A running sync's files stay where they are; this is the one undoable step."""
    script = _build_workspace_discard_argv("mngr", _AGENT_ID, _spec())[3]
    assert 'rm -rf "$HOME"/inactive_synced_folders/notes' in script
    assert f'rm -rf "$HOME"/{WORKSPACE_SYNC_DIRECTORY}/' not in script


@pytest.mark.parametrize("unsafe", ["", "..", "a/../../etc", "../escape"])
def test_an_unsafe_workspace_path_is_refused_before_any_script_is_built(unsafe: str) -> None:
    """These cannot arise from a validated share path; the rm -rf is why it is checked anyway."""
    with pytest.raises(FolderSyncError, match="unsafe workspace path"):
        _build_workspace_discard_argv("mngr", _AGENT_ID, _spec(workspace_path=unsafe))


def test_a_workspace_folder_needing_quoting_is_quoted() -> None:
    argv = _build_workspace_prepare_argv("mngr", _AGENT_ID, _spec(workspace_path="my notes; rm -rf /"))
    assert "'my notes; rm -rf /'" in argv[3]


def test_sink_prefers_mngrs_own_verdict_over_the_raw_event_stream() -> None:
    """mngr's own verdict reads better than the whole event stream."""
    sink = _PairEventSink()
    sink.on_output(json.dumps({"event": "pair_started", "source_path": "/a", "target_path": "/b"}), True)
    sink.on_output(
        json.dumps({"event": "error", "error_class": "MngrError", "message": "Agent directory does not exist: /a"}),
        True,
    )
    sink.on_exit(1)
    assert sink.state == FolderSyncState.FAILED
    assert sink.message == "Agent directory does not exist: /a"


def test_a_directory_minds_had_to_create_is_paired_without_its_old_archive() -> None:
    """Turn a sync off, delete its copy, turn it on: unison would otherwise refuse to start.

    The archive from the earlier pairing still describes files, so unison sees a
    replica that has been emptied and aborts rather than propagate what looks
    like a mass deletion -- taking the user's local folder with it.
    """
    assert "--ignore-archives" in _build_pair_argv("mngr", _spec(), _FRESH_TARGET)


def test_a_resumed_directory_keeps_its_archive() -> None:
    """The copy is back where it was, so the archive describes it correctly and earns its keep."""
    assert "--ignore-archives" not in _build_pair_argv("mngr", _spec(), _TARGET)


def test_turning_a_sync_on_says_whether_it_resumed_or_started_fresh() -> None:
    """The caller cannot tell from the directory alone, and unison has to be told."""
    script = _build_workspace_prepare_argv("mngr", _AGENT_ID, _spec())[3]
    assert "MNGR_MINDS_SYNC=resumed" in script
    assert "MNGR_MINDS_SYNC=fresh" in script
