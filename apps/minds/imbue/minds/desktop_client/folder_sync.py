"""Keeping a shared path level with the workspace, via ``mngr pair``.

Not a second way of handing a path to an agent: an option *on* the first one.
A shared path (:mod:`webdav`) lets the agent reach into this computer over
HTTP; turning sync on for that same path additionally gives the workspace its
own copy and keeps the two the same, so the agent's ordinary tools see
ordinary local files. Nothing here asks for a path -- the caller passes one
the user already shared.

Directories only. ``mngr pair`` syncs a directory, and unison -- which it
drives -- has no native notion of syncing one file; the workarounds cost more
than the rarer case is worth.

One ``mngr pair`` subprocess per sync, supervised here:

* it is spawned with ``--no-require-git``, so pairing never touches either
  side's git state -- no branch checkout, no fetch, no stash. A sync moves
  files and nothing else;
* the user picks the direction and, for a two-way sync, which side wins a
  conflict. Those map onto ``--sync-direction`` and ``--conflict``;
* its ``--format jsonl`` stream is read line by line to drive the state one
  sync is shown in: STARTING until ``pair_syncing`` says unison is watching
  both replicas, then SYNCING until the process exits.

A sync runs only while Minds does, but the *choice* to keep a folder synced
outlives it: :mod:`folder_sync_store` records it, and :meth:`restore_all`
starts them again at launch. Quitting stops every sync; starting brings back
every one the user had not turned off.

Starting a sync checks only what the next step needs -- an absolute path to a
directory that exists -- and deliberately not what a share path goes through:
see :func:`_build_folder_sync_spec` for why that was not the boundary it read
as. The workspace side is not a choice: ``~/synced_folders/<device id>/<the whole local path>`` on the machine,
created first (``mngr pair`` refuses to start on a directory that is not
there). Each part of that earns its place -- the home directory rather than the
working directory, so synced files stay out of the agent's git checkout where
they have no business turning up as untracked changes; the device id, because
one workspace can be synced with from more than one computer, and the same
absolute path on two of them names different directories; and the whole local
path rather than the folder's name, so two same-named folders cannot collide.
"""

import json
import os
import shlex
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.desktop_client.folder_sync_store import FolderSyncRecord
from imbue.minds.desktop_client.folder_sync_store import FolderSyncStore
from imbue.minds.desktop_client.mngr_command import extract_exec_failure_detail
from imbue.minds.desktop_client.mngr_command import extract_exec_stdout
from imbue.minds.desktop_client.mngr_command import run_mngr_to_completion
from imbue.minds.errors import FolderSyncError
from imbue.minds.errors import FolderSyncStoreError
from imbue.minds.errors import MngrCommandError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_for_value

# How long ``stop`` waits for ``mngr pair`` to tear its unison down before
# the process is killed outright. Generous because the teardown crosses the
# network for a workspace on a remote host.
_STOP_TIMEOUT_SECONDS: Final[float] = 10.0

# Events of ``mngr pair --format jsonl`` this manager reads (see libs/mngr_pair):
# the one that says unison is up, and mngr's own fatal-error report.
_PAIR_SYNCING_EVENT: Final[str] = "pair_syncing"
_PAIR_TRANSFERRING_EVENT: Final[str] = "pair_transferring"
_PAIR_ERROR_EVENT: Final[str] = "error"

# Where synced paths land in the workspace, relative to the agent's home. One
# subdirectory per desktop lives under it. Deliberately outside the working
# directory: that is a git repository, and files the user is syncing in from
# their desktop have no business showing up as untracked changes in the agent's
# checkout.
WORKSPACE_SYNC_DIRECTORY: Final[str] = "synced_folders"

# Where a copy goes when the user turns its sync off. Set aside rather than
# deleted, because "stop syncing this" and "throw away the machine's copy" are
# different intentions and the second one is not undoable. Same layout as the
# active directory, so a copy moves between the two by rename alone.
WORKSPACE_INACTIVE_SYNC_DIRECTORY: Final[str] = "inactive_synced_folders"

# What the workspace-side scripts below print to say what they found, since a
# script that did nothing because the directory was already gone exits 0 just
# like one that did the work.
_WORKSPACE_OUTCOME_PREFIX: Final[str] = "MNGR_MINDS_SYNC="

# How many output lines one sync keeps, so a failure can quote mngr's verdict
# rather than only reporting that the process ended.
_RECENT_OUTPUT_LINES: Final[int] = 8

# How many moves one worker will make towards a destination that has not
# changed. Three destinations need at most two moves to reach any other, so
# more than a handful against one unchanged destination is a spin rather than
# progress -- the bug this bounds has happened: a failed activation used to
# read as "not started yet" and be retried forever. Deliberately counted per
# destination rather than per worker: a user flipping the checkbox is entitled
# to as many moves as they ask for, and each of their clicks is a new
# destination that resets the count.
_MAX_STEPS_PER_DESTINATION: Final[int] = 8

# How long the ``mkdir -p`` that makes the workspace side of a sync exist may
# take. It is one ``mngr exec`` round trip, which crosses the network for a
# workspace on a remote host.
_WORKSPACE_MKDIR_TIMEOUT_SECONDS: Final[float] = 60.0

# How long :meth:`restore_all` waits for discovery to say which machine a
# remembered workspace runs on. It starts as soon as the app does, and which
# machine a workspace runs on comes from the discovery pipeline's first pass --
# so at launch the answer is reliably "not yet", and giving up on the first
# look meant no remembered sync ever came back. Generous because the cost of
# being wrong is a sync that stays off until the user notices; the wait is on
# its own thread and blocks nothing.
_RESTORE_HOST_WAIT_SECONDS: Final[float] = 120.0
_RESTORE_HOST_POLL_SECONDS: Final[float] = 1.0


class FolderSyncState(UpperCaseStrEnum):
    """Where one folder actually is right now, as opposed to where the user asked it to be.

    The user sets the destination -- see :class:`FolderSyncActivity`, which is
    what a click writes -- and this is the app's progress towards it. Every
    move between destinations crosses the network, so each has a state for
    being under way, and a click never waits for one.

    STARTING covers everything between spawning ``mngr pair`` and unison
    reporting for duty: resolving the workspace, moving a set-aside copy back,
    installing a usable unison on a remote host, the SSH handshake. RESTARTING
    is the same work for a sync whose settings changed under it -- told apart
    because the files are already on the machine, so the wait is a handshake
    rather than a first copy, and a user who has just changed a dropdown is
    owed the difference. After that a sync alternates between SYNCING (moving
    bytes right now) and SYNCED (up and watching, with nothing to carry across)
    -- which is where it spends nearly all its time. DEACTIVATING and
    DISCARDING are the same idea in the other direction: the sync has stopped
    and the machine is being told what to do with its copy.

    Three states are terminal, and they are worth keeping apart because the
    user is owed a different thing by each. STOPPED means it ended because it
    was asked to -- the user unticked the box, or Minds quit -- and is where a
    settled inactive folder rests, so the unticked checkbox says everything and
    the row needs no badge. FAILED means it ended on its own, and carries the
    reason. UNKNOWN is neither: the folder is one the user still wants synced,
    nothing is running, and Minds cannot say why. That is not a resting place,
    it is a disagreement between what the app says and what it is doing -- and
    it used to be spelled STOPPED, which made it invisible.
    """

    STARTING = auto()
    RESTARTING = auto()
    SYNCING = auto()
    SYNCED = auto()
    DEACTIVATING = auto()
    DISCARDING = auto()
    STOPPED = auto()
    UNKNOWN = auto()
    FAILED = auto()


class FolderSyncSpec(FrozenModel):
    """What one sync syncs, and how."""

    agent_id: str = Field(frozen=True, description="Workspace this path is synced with, as the UI names it")
    host_id: str = Field(
        frozen=True,
        description="The machine that workspace runs on, which is what the sync is actually with",
    )
    local_path: str = Field(frozen=True, description="Absolute shared path on this computer, within the share roots")
    workspace_path: str = Field(
        frozen=True,
        description=(
            f"Where this lands under ~/{WORKSPACE_SYNC_DIRECTORY}: this computer's device id, "
            "then the local path without its leading slash"
        ),
    )
    direction: FolderSyncDirection = Field(frozen=True, description="Which way changes move")
    conflict: FolderSyncConflict = Field(frozen=True, description="Which side wins a conflict (two-way syncs only)")


class _DesiredSync(FrozenModel):
    """Where one folder should end up, and the settings to get it there."""

    spec: FolderSyncSpec = Field(frozen=True, description="What to sync, and how, when it is syncing")
    activity: FolderSyncActivity = Field(frozen=True, description="The destination the user last chose")


def _key_for(spec: "FolderSyncSpec") -> "_SyncKey":
    """The key one spec belongs under. A spec already names both halves of it."""
    return _SyncKey(agent_id=spec.agent_id, local_path=spec.local_path)


class _SyncKey(FrozenModel):
    """What identifies one sync: a folder on this computer, and the workspace it is synced with.

    The workspace is half the identity, not decoration. Two workspaces may keep
    their own copy of one folder -- they land under different machines' home
    directories and cannot collide -- so everything about a sync is held per
    pair. Keyed by the local path alone, the second workspace's sync would
    overwrite the first's destination and take over its process.
    """

    agent_id: str = Field(frozen=True, description="Workspace this folder is synced with")
    local_path: str = Field(frozen=True, description="Absolute path of the folder on this computer")


class _WorkspaceTarget(FrozenModel):
    """Where a sync lands on the machine, and whether anything was already there.

    ``is_resumed`` is False when the directory had to be created, which is the
    case unison must be told about: an archive from a previous pairing of these
    same two paths would describe files that are no longer there, and unison
    refuses to run at all rather than propagate what looks like a mass deletion.
    """

    path: str = Field(frozen=True, description="Absolute path of the sync's directory on the machine")
    is_resumed: bool = Field(
        frozen=True, description="True when a set-aside copy was moved back, False when the directory is new"
    )


class FolderSyncStatus(FrozenModel):
    """One sync as the UI sees it: what it syncs plus where it has got to."""

    spec: FolderSyncSpec = Field(frozen=True, description="The sync's settings")
    state: FolderSyncState = Field(frozen=True, description="How far along it is")
    message: str = Field(default="", description="Why it failed, when it did; empty otherwise")
    bytes_done: int = Field(default=0, description="Bytes carried across so far in the transfer running now")
    bytes_total: int = Field(default=0, description="Bytes that transfer set out to carry; 0 when none is running")


def _mngr_sync_direction(direction: FolderSyncDirection) -> str:
    """The ``--sync-direction`` value for ``direction``.

    ``mngr pair`` names its replicas from the agent's point of view: the
    *source* is the workspace and the *target* is this computer. So its
    "forward" is workspace-to-here and its "reverse" is here-to-workspace.
    """
    match direction:
        case FolderSyncDirection.BOTH:
            return "both"
        case FolderSyncDirection.TO_WORKSPACE:
            return "reverse"
        case _ as unreachable:
            assert_never(unreachable)


def _mngr_conflict_mode(conflict: FolderSyncConflict) -> str:
    """The ``--conflict`` value for ``conflict`` (source is the workspace; target is here)."""
    match conflict:
        case FolderSyncConflict.NEWER:
            return "newer"
        case FolderSyncConflict.THIS_COMPUTER:
            return "target"
        case FolderSyncConflict.WORKSPACE:
            return "source"
        case _ as unreachable:
            assert_never(unreachable)


def _build_pair_argv(mngr_binary: str, spec: FolderSyncSpec, workspace_target: "_WorkspaceTarget") -> list[str]:
    """The ``mngr pair`` command line for one sync.

    ``workspace_target`` is the absolute path the sync lands on, resolved by
    :meth:`FolderSyncManager._prepare_workspace_target` -- it sits under the
    agent's home rather than its working directory, so synced files never show
    up as untracked changes in the agent's checkout.

    Addressed by host rather than by agent: what a sync needs is the machine
    the directory is on, and requiring an agent would make a sync depend on one
    existing and be resolved through it for no reason.

    ``--no-require-git`` is not a user choice: a sync is about files, and
    leaving git out keeps it from checking out branches or merging commits
    in either repository behind the user's back. ``--no-start`` is not one
    either: restoring syncs is something Minds does on its own at startup, and
    it must never be what turns a stopped machine on (and bills for it). Nor is
    ``--no-links``: the two sides are different computers, so a symlink does
    not mean the same thing on both.
    """
    return [
        mngr_binary,
        "pair",
        "--source-host",
        spec.host_id,
        "--source-path",
        workspace_target.path,
        "--target",
        spec.local_path,
        "--no-require-git",
        "--no-start",
        # Symlinks inside the folder stay on the side they are on. An absolute
        # one names a path that need not exist on the other machine, and a
        # relative one can point out of the folder, where the two sides do not
        # agree what it reaches -- so carrying it across would either arrive
        # broken or quietly widen what the sync touches.
        "--no-links",
        # A folder that is a git repository is synced without its history.
        # ``.git`` is a database whose invariants span files, and this sync has
        # no notion of a transaction: it would copy ``index.lock`` across and
        # block git on the other side, and land refs pointing at objects that
        # had not arrived yet. ``mngr pair``'s own git-reconciling mode would
        # keep unison out of ``.git`` for its own reasons, but this sync does
        # not use that mode -- so it asks, rather than inheriting the answer.
        "--exclude",
        ".git",
        # A directory Minds just created has no shared history with this
        # folder, whatever an archive from an earlier pairing of the same two
        # paths says. Without this unison sees a replica that used to hold
        # files and now holds none, and refuses to start at all -- which is
        # what turning a sync off, deleting its copy, and turning it on again
        # used to produce.
        *(() if workspace_target.is_resumed else ("--ignore-archives",)),
        "--sync-direction",
        _mngr_sync_direction(spec.direction),
        "--conflict",
        _mngr_conflict_mode(spec.conflict),
        "--format",
        "jsonl",
    ]


def _build_workspace_prepare_argv(mngr_binary: str, agent_address: str, spec: FolderSyncSpec) -> list[str]:
    """The ``mngr exec`` command that makes room for a sync and says where that is.

    Does two things in one round trip, because it needs two answers from the
    same place: it creates the directory the sync lands in, and prints the
    agent's home directory, which is what the landing path is relative to.
    Minds cannot know that home from here -- the agent may be on another
    machine entirely -- and ``mngr exec`` runs where it can simply ask.

    """
    return _exec_argv(mngr_binary, agent_address, _workspace_activate_script(spec))


def _workspace_paths(spec: FolderSyncSpec) -> tuple[str, str]:
    """The active and set-aside paths of one sync on the machine, shell-quoted.

    ``workspace_path`` must not be empty or contain a ``..`` segment, which is
    what makes it safe to interpolate into the ``rm -rf`` in
    :func:`_build_workspace_discard_argv`. Nothing upstream guarantees that --
    :func:`_build_folder_sync_spec` checks only that the path is absolute and
    is a directory -- so this is the check, not a restatement of one, and the
    cost of it being wrong is somebody else's files.
    """
    workspace_path = spec.workspace_path
    if not workspace_path or any(segment == ".." for segment in workspace_path.split("/")):
        raise FolderSyncError(f"Refusing to act on an unsafe workspace path: {workspace_path!r}")
    leaf = shlex.quote(workspace_path)
    return f'"$HOME"/{WORKSPACE_SYNC_DIRECTORY}/{leaf}', f'"$HOME"/{WORKSPACE_INACTIVE_SYNC_DIRECTORY}/{leaf}'


def _exec_argv(mngr_binary: str, agent_address: str, script: str) -> list[str]:
    """One ``mngr exec`` of ``script`` on the workspace, as JSON."""
    return [mngr_binary, "exec", agent_address, script, "--no-start", "--format", "json"]


def _workspace_activate_script(spec: FolderSyncSpec) -> str:
    """Make the sync's directory exist and ready to pair with, then report ``$HOME``.

    A copy set aside by an earlier turn-off is moved back, so turning sync on
    again resumes from the files that were there rather than re-fetching them.
    Everything about that is best-effort by design: the copy may be gone (an
    agent owns its own filesystem and may have deleted it), in which case this
    simply creates an empty directory and the sync fills it. If somehow both
    exist, the active one is what pairing uses and the set-aside one is left
    alone rather than being overwritten.
    """
    active, inactive = _workspace_paths(spec)
    return "\n".join(
        (
            "set -e",
            f"if [ -e {inactive} ] && [ ! -e {active} ]; then",
            f'  mkdir -p "$(dirname {active})"',
            f"  mv {inactive} {active}",
            "fi",
            f"if [ -e {active} ]; then",
            f'  printf "%s\\n" "{_WORKSPACE_OUTCOME_PREFIX}resumed"',
            "else",
            f'  printf "%s\\n" "{_WORKSPACE_OUTCOME_PREFIX}fresh"',
            "fi",
            f"mkdir -p {active}",
            'printf %s "$HOME"',
        )
    )


def _build_workspace_deactivate_argv(mngr_binary: str, agent_address: str, spec: FolderSyncSpec) -> list[str]:
    """The ``mngr exec`` that sets a sync's copy aside when its sync is turned off.

    A rename within the machine's home, so it costs no copying however large
    the folder is. An already-absent copy is not a failure -- the agent may
    have deleted it -- and anything sitting in the destination is removed
    first: nothing but Minds is supposed to write under
    ``~/inactive_synced_folders``, so something there is a mistake rather than
    a file to preserve, and leaving it would strand this copy instead.
    """
    active, inactive = _workspace_paths(spec)
    script = "\n".join(
        (
            "set -e",
            f"if [ ! -e {active} ]; then",
            f'  printf "%s" "{_WORKSPACE_OUTCOME_PREFIX}absent"',
            "else",
            f'  mkdir -p "$(dirname {inactive})"',
            f"  rm -rf {inactive}",
            f"  mv {active} {inactive}",
            f'  printf "%s" "{_WORKSPACE_OUTCOME_PREFIX}moved"',
            "fi",
        )
    )
    return _exec_argv(mngr_binary, agent_address, script)


def _build_workspace_discard_argv(mngr_binary: str, agent_address: str, spec: FolderSyncSpec) -> list[str]:
    """The ``mngr exec`` that deletes a set-aside copy for good.

    Only ever the set-aside directory: a sync that is still running keeps its
    files where they are, and this is the one operation here that cannot be
    undone.
    """
    _active, inactive = _workspace_paths(spec)
    script = "\n".join(
        (
            "set -e",
            f"rm -rf {inactive}",
            f'printf "%s" "{_WORKSPACE_OUTCOME_PREFIX}discarded"',
        )
    )
    return _exec_argv(mngr_binary, agent_address, script)


def _expand_home(path: str, home_dir: Path) -> str:
    """Expand a leading ``~`` / ``~/`` against ``home_dir``.

    A pure string splice rather than ``Path`` joining, so nothing in the
    remainder is quietly normalized on the way through: what the sync is told
    to sync is what it syncs. ``~user`` is another user's home, which this
    cannot resolve, so it is left alone to fail the absolute-path check below.
    """
    if path == "~" or path.startswith("~/"):
        return f"{home_dir}{path[1:]}"
    return path


def _build_folder_sync_spec(
    agent_id: str,
    host_id: str,
    raw_local_path: str,
    direction: FolderSyncDirection,
    conflict: FolderSyncConflict,
    home_dir: Path,
    device_id: str,
) -> FolderSyncSpec:
    """Make the local side of a sync from a path, or raise :class:`FolderSyncError`.

    Sanity checks, not a security boundary, and deliberately not the ones a
    share path goes through. Running those here read as though a sync could
    not reach somewhere a share could not -- which was never true and could
    not be made true: the caller with the strongest reason to be checked is
    ``restore_all``, reading a file that sits beside this app's own signing key
    and latchkey credentials. Anything able to edit that file can read those
    and reach the workspace directly, so a path check on the way out defends
    against an attacker who has already won, while suggesting a perimeter that
    is not there.

    What is left is what the next step actually needs: an absolute path,
    because the workspace side is built by splicing it, and a directory that
    exists, because ``mngr pair`` refuses one that does not and a clear row
    beats a subprocess dying seconds later.
    """
    local_path = _expand_home(raw_local_path.strip(), home_dir)
    if not local_path.startswith("/"):
        raise FolderSyncError(f"A folder to sync must be given as an absolute path: {raw_local_path}")
    resolved = Path(local_path)
    if not resolved.exists():
        raise FolderSyncError(f"There is nothing at {local_path} on this computer.")
    if not resolved.is_dir():
        raise FolderSyncError(f"Only folders can be synced, and {local_path} is a file.")
    return FolderSyncSpec(
        agent_id=agent_id,
        host_id=host_id,
        local_path=local_path,
        # The workspace side is not a choice: it mirrors the whole local path
        # under this computer's own directory, so /Users/me/notes from device
        # host-abc lands at ~/synced_folders/host-abc/Users/me/notes. The whole
        # path, because a bare name would collide the moment someone syncs two
        # folders sharing one; under the device id, because one workspace can be
        # synced with from more than one computer, and two of them may well have
        # the same absolute path meaning different directories.
        workspace_path=f"{device_id}/{local_path.lstrip('/')}",
        direction=direction,
        conflict=conflict,
    )


class _PairEventSink(MutableModel):
    """The mutable state of one sync, fed by its subprocess's output.

    Separate from :class:`_FolderSyncRun` so that the row has somewhere to
    carry state before there is a process: a sync exists, and is shown, from
    the moment the user asks for it, while bringing it up takes seconds.
    """

    state: FolderSyncState = Field(default=FolderSyncState.STARTING, description="How far along the sync is")
    message: str = Field(default="", description="Why it failed, when it did; empty otherwise")
    bytes_done: int = Field(default=0, description="Bytes carried across so far in the transfer running now")
    bytes_total: int = Field(default=0, description="Bytes that transfer set out to carry; 0 when none is running")
    recent_output: tuple[str, ...] = Field(default=(), description="Tail of what the subprocess wrote")
    error_message: str = Field(default="", description="mngr's own verdict, from its ``error`` event")
    is_stop_requested: bool = Field(
        default=False, description="The user asked for this sync to stop, so its exit is not a failure"
    )
    _left_starting: threading.Event = PrivateAttr(default_factory=threading.Event)

    def wait_until_started(self, timeout: float) -> None:
        """Block until this sync stops being STARTING, or until ``timeout`` elapses."""
        self._left_starting.wait(timeout=timeout)

    def on_output(self, line: str, is_stdout: bool) -> None:
        """Fold one line of the subprocess's output into the sync's state.

        ``pair_syncing`` says unison is up, which settles the sync at SYNCED;
        ``pair_transferring`` then moves it between SYNCED and SYNCING as bytes
        actually move. mngr's ``error`` event is kept as the verdict a failure
        is reported with. Every line joins the tail regardless -- mngr's own
        logging, on stderr, is what a death neither of those events explains is
        diagnosed from.
        """
        stripped = line.strip()
        logger.debug("mngr pair: {}", stripped)
        if stripped:
            self.recent_output = (*self.recent_output, stripped)[-_RECENT_OUTPUT_LINES:]
        event = _parse_pair_event(stripped) if is_stdout else None
        if event is None:
            return
        if event.get("event") == _PAIR_SYNCING_EVENT and self.state in (
            FolderSyncState.STARTING,
            FolderSyncState.RESTARTING,
        ):
            # Up and watching, with nothing to carry across until told otherwise.
            self.state = FolderSyncState.SYNCED
            self._left_starting.set()
        if event.get("event") == _PAIR_TRANSFERRING_EVENT and self.state in (
            FolderSyncState.SYNCED,
            FolderSyncState.SYNCING,
        ):
            is_transferring = event.get("is_transferring")
            self.state = FolderSyncState.SYNCING if is_transferring is True else FolderSyncState.SYNCED
            # unison's own count of the transfer it is part-way through, which
            # rides along on the same event. Cleared when it settles, so a
            # finished sync never shows a half-finished number.
            done, total = event.get("bytes_done"), event.get("bytes_total")
            is_counted = is_transferring is True and isinstance(done, int) and isinstance(total, int)
            self.bytes_done = done if is_counted and isinstance(done, int) else 0
            self.bytes_total = total if is_counted and isinstance(total, int) else 0
        if event.get("event") == _PAIR_ERROR_EVENT:
            message = event.get("message")
            if isinstance(message, str) and message:
                self.error_message = message

    def on_exit(self, exit_code: int) -> None:
        """Settle the sync's final state once its subprocess is gone."""
        if self.is_stop_requested or exit_code == 0:
            self.on_stopped()
            return
        self.state = FolderSyncState.FAILED
        self.message = _failure_message(exit_code, self.error_message, self.recent_output)
        self._left_starting.set()

    def on_failed(self, message: str) -> None:
        """Settle a sync that never got off the ground, with the reason."""
        self.state = FolderSyncState.FAILED
        self.message = message
        self._left_starting.set()

    def on_stopped(self) -> None:
        """Settle the state of a sync that ended because it was asked to."""
        self.state = FolderSyncState.STOPPED
        self.message = ""
        self._left_starting.set()


class _FolderSyncRun(MutableModel):
    """One sync: its settings, its ``mngr pair`` subprocess, and that sync's state."""

    model_config = {"arbitrary_types_allowed": True}

    spec: FolderSyncSpec = Field(frozen=True, description="The sync's settings")
    sink: _PairEventSink = Field(frozen=True, description="State fed by the subprocess's output")
    process: RunningProcess | None = Field(
        default=None,
        description="The supervising ``mngr pair`` subprocess; None until the sync has been brought up",
    )

    def watch(self) -> None:
        """Block until the subprocess exits, then settle the sync's final state.

        Runs on its own thread for the life of the sync. The concurrency group
        does not check this process, so a nonzero exit arrives here as a return
        value rather than as a raised ``ProcessError``.
        """
        process = self.process
        if process is None:
            return
        try:
            exit_code = process.wait()
        except ConcurrencyGroupError as e:
            logger.warning("Could not wait on the folder sync for {}: {}", self.spec.local_path, e)
            self.sink.state = FolderSyncState.FAILED
            self.sink.message = str(e)
            return
        logger.info("Folder sync for {} ended with code {}", self.spec.local_path, exit_code)
        self.sink.on_exit(exit_code)

    def terminate(self) -> None:
        """SIGTERM the subprocess and wait for it to tear its unison down.

        ``mngr pair`` treats SIGTERM as Ctrl+C, so this is the same clean stop
        a person gets at a terminal; the concurrency group kills a process that
        ignores it once ``_STOP_TIMEOUT_SECONDS`` is up.

        Settles the state here rather than leaving it to :meth:`watch`. Both
        would say the same thing, but the watcher runs on its own thread and
        may not have got there yet when the caller renders the row -- which
        would leave a stopped sync still reading "starting".
        """
        self.sink.is_stop_requested = True
        process = self.process
        if process is None:
            # Nothing spawned yet: ``_bring_up`` sees the flag and gives up.
            self.sink.on_stopped()
            return
        if not process.is_finished():
            try:
                process.terminate(force_kill_seconds=_STOP_TIMEOUT_SECONDS)
            except (OSError, ConcurrencyGroupError) as e:
                # The process may still be up, so the row keeps whatever state
                # it had rather than claiming a stop that did not happen.
                logger.warning("Could not stop the folder sync for {}: {}", self.spec.local_path, e)
                return
        self.sink.on_stopped()

    def is_live(self) -> bool:
        """Whether this sync still holds its path (starting, or up and running)."""
        return self.sink.state in (
            FolderSyncState.STARTING,
            FolderSyncState.RESTARTING,
            FolderSyncState.SYNCING,
            FolderSyncState.SYNCED,
        )

    def to_status(self) -> FolderSyncStatus:
        return FolderSyncStatus(
            spec=self.spec,
            state=self.sink.state,
            message=self.sink.message,
            bytes_done=self.sink.bytes_done,
            bytes_total=self.sink.bytes_total,
        )


def _parse_pair_event(line: str) -> dict[str, object] | None:
    """One stdout line of ``mngr pair --format jsonl`` as an event, or None.

    None for anything that is not a JSON object: mngr writes only JSON to
    stdout in jsonl mode, so that is a truncated line rather than content. The
    tail keeps it either way.
    """
    if not line.startswith("{"):
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        # A line that opens with "{" but does not parse is corruption, not
        # content: in jsonl mode mngr writes nothing else to stdout.
        logger.warning("Unparseable line from mngr pair: {}", line[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def _failure_message(exit_code: int, error_message: str, recent_output: tuple[str, ...]) -> str:
    """What to tell the user about a sync that ended on its own.

    mngr's own verdict when it reported one -- "Agent directory does not
    exist: ..." reads far better than the whole event stream. The tail is the
    fallback for a death nobody anticipated, where it is the only diagnosis
    there is.
    """
    if error_message:
        return error_message
    tail = " ".join(recent_output).strip()
    if tail:
        return f"The sync stopped unexpectedly (exit code {exit_code}): {tail}"
    return f"The sync stopped unexpectedly (exit code {exit_code})."


def _is_within(candidate: str, other: str) -> bool:
    """Whether ``candidate`` is ``other`` itself, or a directory inside it.

    Purely lexical, on paths both sides have already normalized. That is
    deliberate rather than a shortcut: what this guards is a collision between
    the two *workspace* paths, which are built by splicing these strings under
    a common root, so string containment is exactly the question. A local path
    whose components are symlinks can still reach a folder some other sync also
    covers, and nothing here notices -- see the note on
    :func:`_overlapping_path_for_agent`.

    Case-insensitively. The usual macOS volume is case-insensitive, so
    ``~/Work`` and ``~/work/notes``
    are a real parent and child there while comparing as unrelated -- and what
    that misses is not a warning but the refusal that stops two copies nesting
    on the machine, which destroys the folder on this computer. Over-refusing a
    genuinely distinct pair on a case-sensitive volume is the safer way to be
    wrong.
    """
    lower_candidate, lower_other = candidate.lower(), other.rstrip("/").lower()
    return lower_candidate == lower_other or lower_candidate.startswith(f"{lower_other}/")


def _overlaps(one_path: str, other_path: str) -> bool:
    """Whether two local paths name the same folder or one inside the other."""
    return _is_within(one_path, other_path) or _is_within(other_path, one_path)


class FolderSyncManager(MutableModel):
    """Owns every running folder sync for this desktop client.

    Keyed by local folder: one folder on this computer syncs with at most one
    workspace, which keeps two syncs from writing the same files at each other.
    """

    model_config = {"arbitrary_types_allowed": True}

    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Group owning the ``mngr pair`` subprocesses and their watcher threads"
    )
    mngr_binary: str = Field(default=MNGR_BINARY, frozen=True, description="Path/name of the mngr binary")
    mngr_host_dir: Path = Field(
        default_factory=lambda: Path.home() / ".mngr", frozen=True, description="MNGR_HOST_DIR for the subprocesses"
    )
    home_dir: Path = Field(
        default_factory=Path.home, frozen=True, description="Expansion target for a leading ``~`` in a local path"
    )
    device_id: str = Field(
        frozen=True,
        description="This install's stable device id, which the workspace side of every sync is filed under",
    )
    restore_host_wait_seconds: float = Field(
        default=_RESTORE_HOST_WAIT_SECONDS,
        description=(
            "How long restore_all waits at launch for discovery to say which machine a remembered "
            "workspace runs on. Injected so a test can assert what happens when it runs out"
        ),
    )
    store: FolderSyncStore | None = Field(
        default=None,
        description=(
            "Where the user's standing choice to keep a folder synced is remembered, so it survives a "
            "restart. None in a build or a test with nothing to remember it in."
        ),
    )
    backend_resolver: BackendResolverInterface = Field(
        frozen=True,
        description="Says which machine a workspace runs on, which is the side a sync pairs with",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _runs_by_key: dict[_SyncKey, _FolderSyncRun] = PrivateAttr(default_factory=dict)
    # What one folder is in the middle of, while it is in the middle of it. The
    # user's destination is in the store; this is how far the app has got, and
    # it is what the row shows until the folder settles.
    _in_flight_by_key: dict[_SyncKey, FolderSyncState] = PrivateAttr(default_factory=dict)
    # One lock per folder, so its workspace-side moves happen in the order they
    # were asked for. Turning sync off and straight back on is two renames of
    # the same directory, and running them at once would lose it.
    _workspace_locks: dict[_SyncKey, threading.Lock] = PrivateAttr(default_factory=dict)
    # Where each folder should end up, which is what a click writes.
    _desired_by_key: dict[_SyncKey, _DesiredSync] = PrivateAttr(default_factory=dict)
    # What the converger last carried out, which is how it knows it is done.
    _applied_by_key: dict[_SyncKey, _DesiredSync] = PrivateAttr(default_factory=dict)
    # Folders with a worker walking them somewhere; a second click adds none.
    _converging: set[_SyncKey] = PrivateAttr(default_factory=set)
    # Set when a folder's worker retires, for a caller that waits on the outcome.
    _settled_events: dict[_SyncKey, threading.Event] = PrivateAttr(default_factory=dict)
    # Why a remembered sync could not be brought back at launch. Kept because
    # the alternative is a row that says nothing: the record still says ACTIVE,
    # so the checkbox is on, and with no run behind it the row would otherwise
    # report a bare STOPPED with no reason and no way to act on it.
    _restore_failures: dict[_SyncKey, str] = PrivateAttr(default_factory=dict)

    def start(
        self,
        agent_id: str,
        raw_local_path: str,
        direction: FolderSyncDirection,
        conflict: FolderSyncConflict,
    ) -> FolderSyncStatus:
        """Ask for ``local_path`` to be kept synced, and report where it is now.

        Returns at once, in STARTING. Getting there means an ``mngr exec`` round
        trip and then waiting on ``mngr pair``, which together take seconds --
        long enough that doing it here would freeze the pane on the click that
        asked for it.

        Raises :class:`FolderSyncError` only for what can be judged
        immediately: a path that is refused, or a workspace Minds cannot place.
        """
        spec = self._spec_for(agent_id, raw_local_path, direction, conflict)
        refusal = self._reason_to_refuse(spec.local_path, agent_id)
        if refusal is not None:
            raise FolderSyncError(refusal)
        self._forget_restore_failure(_key_for(spec))
        self._set_desired(spec, FolderSyncActivity.ACTIVE)
        return self.status_for_path(agent_id, spec.local_path) or FolderSyncStatus(
            spec=spec, state=FolderSyncState.STARTING, message=""
        )

    def reason_sync_is_unavailable(self, agent_id: str, raw_local_path: str) -> str:
        """Why this folder cannot be synced with this workspace, or empty when it can.

        Exactly the question :meth:`start` asks, asked without doing anything,
        so the pane can grey the checkbox out and say why instead of letting
        the user tick it and be refused. The two go through the same two
        checks -- building the spec, then :meth:`_reason_to_refuse` -- because
        a pane that greys out a different set of folders than ``start``
        rejects is worse than no greying at all: it would either refuse
        something the user could have had, or promise something that then
        fails on the click.

        The settings are stand-ins: what makes a folder ineligible is the
        folder and the workspace, never which way changes travel or who wins a
        clash.
        """
        try:
            spec = self._spec_for(agent_id, raw_local_path, FolderSyncDirection.TO_WORKSPACE, FolderSyncConflict.NEWER)
        except FolderSyncError as e:
            return str(e)
        return self._reason_to_refuse(spec.local_path, agent_id) or ""

    def _reason_to_refuse(self, local_path: str, agent_id: str) -> str | None:
        """Why ``local_path`` cannot be synced with ``agent_id`` right now, or None.

        Two reasons, and they are unrelated.

        **This workspace already syncs a folder that overlaps.** The two
        workspace paths would nest, so one sync's copy would sit inside the
        other's replica. That is not a question of which writes win: each
        sync's own lifecycle breaks the other. Setting the inner copy aside (a
        rename, on the machine) reads to the outer sync as the user deleting
        that directory, and it propagates the deletion back to the folder on
        this computer. Under ``-batch -auto`` nothing asks first, and
        ``confirmbigdel`` does not fire, because only part of the replica went.

        **Another workspace already syncs this exact folder.** Nothing about
        syncing forbids that -- the copies land under different machines' home
        directories and cannot collide -- but everything in this manager is
        keyed by local path alone, so a second sync of one path would overwrite
        the first's destination and take over its process. A limit of the
        bookkeeping, not of the idea; a folder *inside* another workspace's is
        allowed, because the two have different keys.

        A folder whose sync is *off* blocks just as much as one that is on. Its
        copy was not deleted, only renamed into the set-aside tree -- which
        mirrors the active tree exactly, so two overlapping folders nest there
        too. Turning the outer one back on then moves its whole directory
        across, carrying the inner one's set-aside copy inside the outer's live
        replica, where it is propagated onto this computer; and removing the
        outer's copy deletes the inner's with it. Only a folder whose copy has
        been removed for good is out of the way.

        Lexical, so it does not see a folder reached through a symlinked
        component. That is the accepted limit: what is prevented is two copies
        colliding on the machine, and a path Minds has never been told about
        cannot collide with one it has.
        """
        for other_path, activity in self._paths_with_a_copy_for(agent_id).items():
            if other_path == local_path or not _overlaps(local_path, other_path):
                continue
            if activity == FolderSyncActivity.ACTIVE:
                return (
                    f"{other_path} is already synced with this workspace, and one folder is inside the "
                    "other. Two syncs whose folders overlap would land on top of each other on the "
                    "machine. Turn that one off first."
                )
            return (
                f"{other_path} is not syncing, but this workspace still holds a copy of it, and one "
                "folder is inside the other. The two copies would land on top of each other on the "
                "machine. Remove that copy first."
            )
        return None

    def _paths_with_a_copy_for(self, agent_id: str) -> dict[str, FolderSyncActivity]:
        """Every folder this workspace may be holding a copy of, and which tree it is in.

        Both trees count -- see :meth:`_reason_to_refuse` for why a set-aside
        copy is as much in the way as a live one. Only DISCARDED is left out,
        because that is the one state with nothing on disk.

        Read from the store as well as from memory, and not only for tidiness:
        :meth:`restore_all` brings back only ACTIVE records, so a sync the user
        turned off before quitting exists nowhere but the store until something
        touches it. Memory wins where both have an answer, since it is the more
        recent one.
        """
        activity_by_path: dict[str, FolderSyncActivity] = {}
        if self.store is not None:
            for record in self.store.list_for_agent(agent_id):
                activity_by_path[record.local_path] = record.activity
        with self._lock:
            for key, desired in self._desired_by_key.items():
                if key.agent_id == agent_id:
                    activity_by_path[key.local_path] = desired.activity
        return {
            path: activity for path, activity in activity_by_path.items() if activity != FolderSyncActivity.DISCARDED
        }

    def overlapping_paths_in_other_workspaces(self, local_path: str, agent_id: str) -> tuple[str, ...]:
        """Folders other workspaces sync that are this one, or hold it, or sit inside it.

        Allowed, unlike the same-workspace case: the copies land under different
        machines' home directories, so nothing collides on disk and each pairing
        keeps its own history. What the caller warns about is subtler -- the
        same bytes cross the network once per workspace, and a clash is settled
        per pairing, so each row's clash rule decides only its own half of an
        outcome that spans all of them.
        """
        with self._lock:
            desired_by_key = dict(self._desired_by_key)
        return tuple(
            sorted(
                {
                    other_key.local_path
                    for other_key, desired in desired_by_key.items()
                    if desired.activity == FolderSyncActivity.ACTIVE
                    and other_key.agent_id != agent_id
                    and _overlaps(local_path, other_key.local_path)
                }
            )
        )

    def stop(self, agent_id: str, local_path: str) -> FolderSyncStatus:
        """Ask for one workspace's sync of ``local_path`` to stop, and report where it is now.

        Returns at once, in DEACTIVATING. Setting the machine's copy aside is a
        round trip to it, and a click never waits for one.

        Never refuses. Turning something off is a wish about where the folder
        ends up, and the user is entitled to it whatever state the app has got
        itself into -- including one where the sync was never brought up, which
        is the case this used to raise on: a restore that failed left a record
        saying ACTIVE, a ticked checkbox, and nothing to stop. Refusing there
        left the user with a switch that would not move.
        """
        key = _SyncKey(agent_id=agent_id, local_path=local_path)
        spec = self._known_spec_for(key) or self._spec_for_forgotten(agent_id, local_path)
        if spec is None:
            # Nothing is running and nothing is remembered, so there is no
            # folder to move and nothing to record -- which is what being off
            # already looks like.
            return FolderSyncStatus(
                spec=_build_folder_sync_spec(
                    agent_id=agent_id,
                    host_id="",
                    raw_local_path=local_path,
                    direction=FolderSyncDirection.TO_WORKSPACE,
                    conflict=FolderSyncConflict.NEWER,
                    home_dir=self.home_dir,
                    device_id=self.device_id,
                ),
                state=FolderSyncState.STOPPED,
                message="",
            )
        self._forget_restore_failure(key)
        self._set_desired(spec, FolderSyncActivity.INACTIVE)
        return FolderSyncStatus(spec=spec, state=FolderSyncState.DEACTIVATING, message="")

    def retry(self, agent_id: str, local_path: str) -> FolderSyncStatus:
        """Bring a failed sync up again, without the user having to flip the checkbox twice.

        A sync that failed on its own has had everything asked of it carried
        out, so the converger is right to leave it alone -- see
        :meth:`_next_step`. That makes "still ACTIVE, still broken" a resting
        place with no way out but turning the folder off and on again. This is
        that, said once: what was applied is forgotten, so the next pass has
        something to do.

        Raises :class:`FolderSyncError` when nothing is known about the folder.
        """
        key = _SyncKey(agent_id=agent_id, local_path=local_path)
        spec = self._known_spec_for(key) or self._spec_for_forgotten(agent_id, local_path)
        if spec is None:
            raise FolderSyncError(f"Minds has no record of syncing {local_path}, so there is nothing to retry.")
        with self._lock:
            self._applied_by_key.pop(key, None)
            self._runs_by_key.pop(key, None)
        self._forget_restore_failure(key)
        self._set_desired(spec, FolderSyncActivity.ACTIVE)
        return FolderSyncStatus(spec=spec, state=FolderSyncState.STARTING, message="")

    def discard_copy(self, agent_id: str, local_path: str) -> None:
        """Ask for the set-aside copy of a folder whose sync is off to be deleted.

        Returns as soon as the deletion is asked for, like every other move
        here. The one operation that cannot be undone, so it is never part of
        turning sync off -- the user asks for it separately, once they can see
        that a copy is being kept.

        Raises :class:`FolderSyncError` when there is no such record, or when
        the folder is still meant to be syncing: its files are in use, and the
        copy to delete is the set-aside one, which in that case does not exist.
        """
        record = self._record_for(agent_id, local_path)
        if record is None:
            raise FolderSyncError(f"{local_path} has no copy on this workspace's machine.")
        if self.desired_activity_for(agent_id, local_path) == FolderSyncActivity.ACTIVE:
            raise FolderSyncError(f"{local_path} is still syncing; turn syncing off before deleting its copy.")
        self._set_desired(self._spec_for_record(record), FolderSyncActivity.DISCARDED)

    # desired state, and the loop that converges on it
    #
    # Every click records where the folder should end up and returns. One worker
    # per folder then walks it there, re-reading the destination after each
    # step. So a burst of clicks costs one worker and however many moves the
    # last of them actually implies -- clicking off and straight back on while
    # a deactivation is in flight lets that finish, then starts the sync again,
    # rather than queueing four workspace round trips or (as dropping them
    # would) settling somewhere the user did not ask for.

    def _set_desired(self, spec: FolderSyncSpec, activity: FolderSyncActivity) -> None:
        """Record where one sync should end up, and make sure something is walking it there."""
        key = _key_for(spec)
        self._remember(spec, activity)
        with self._lock:
            self._desired_by_key[key] = _DesiredSync(spec=spec, activity=activity)
            if key in self._converging:
                # A worker is mid-step; it re-reads the destination when it lands.
                return
            self._converging.add(key)
            self._settled_events[key] = threading.Event()
        self.concurrency_group.start_new_thread(
            target=self._converge,
            args=(key,),
            name=f"folder-sync-converge-{spec.agent_id}-{spec.local_path}",
            daemon=True,
            is_checked=False,
        )

    def _converge(self, key: _SyncKey) -> None:
        """Thread body: move one sync towards its destination until it is there.

        Retires only after checking, under the lock, that the destination has
        not moved since the last step -- otherwise a click landing in that
        window would find a worker still marked as running and nothing would
        pick it up.
        """
        last_desired: _DesiredSync | None = None
        steps_for_this_destination = 0
        try:
            # Runs for as long as this sync has somewhere to be. There is no
            # step budget for the whole worker: a click is a new destination
            # and is entitled to the moves it implies, however many clicks
            # there have been. What is bounded is the moves against any *one*
            # destination, below.
            while (desired := self._desired_for(key)) is not None:
                if desired != last_desired:
                    # A new destination, so the count starts again.
                    last_desired = desired
                    steps_for_this_destination = 0
                step = self._next_step(key, desired)
                if step is None:
                    # Retiring and the last look at the destination happen under
                    # one lock hold. Apart, a click landing between them would
                    # find a worker still marked as running and be picked up by
                    # nobody.
                    with self._lock:
                        if self._desired_by_key.get(key) is desired:
                            self._converging.discard(key)
                            return
                    continue
                steps_for_this_destination += 1
                if steps_for_this_destination > _MAX_STEPS_PER_DESTINATION:
                    # Not a busy user: the same destination has asked for the
                    # same kind of move too many times over, which no sequence
                    # of real moves does. Giving up is better than a thread
                    # spinning on the machine, and worse than either would be
                    # carrying on silently.
                    logger.warning(
                        "Gave up moving {} after {} moves towards one destination; it is left where it "
                        "got to and the next click starts again",
                        key.local_path,
                        _MAX_STEPS_PER_DESTINATION,
                    )
                    return
                step(desired.spec)
        finally:
            # Also covers a step that raised: the flag must not outlive the
            # thread holding it, or the folder is stuck with no worker.
            with self._lock:
                self._converging.discard(key)
                settled = self._settled_events.pop(key, None)
            if settled is not None:
                settled.set()

    def _desired_for(self, key: _SyncKey) -> _DesiredSync | None:
        """Where this sync should end up, or None once nothing wants it anywhere."""
        with self._lock:
            return self._desired_by_key.get(key)

    def _next_step(self, key: _SyncKey, desired: _DesiredSync) -> Callable[[FolderSyncSpec], None] | None:
        """The one move that gets ``key`` closer to ``desired``, or None when it is there.

        Compares against what was last carried out, not against what is running.
        A sync that failed on its own has still had everything asked of it done,
        so it settles as failed rather than being started again forever; the
        user asking a second time is a new destination and moves it on.
        """
        with self._lock:
            run = self._runs_by_key.get(key)
            applied = self._applied_by_key.get(key)
        is_done = applied is not None and applied.activity == desired.activity and applied.spec == desired.spec
        if desired.activity == FolderSyncActivity.ACTIVE:
            if is_done:
                return None
            # Settings changed under a running sync: the directories are where
            # they belong, so only the unison process has to be replaced.
            return self._restart if run is not None and run.is_live() else self._activate
        # Both remaining destinations start by stopping a sync that is running.
        if run is not None and run.is_live():
            return self._set_copy_aside
        if is_done:
            return None
        if desired.activity == FolderSyncActivity.INACTIVE:
            return self._set_copy_aside
        return self._discard_set_aside_copy

    def _note_applied(self, spec: FolderSyncSpec, activity: FolderSyncActivity) -> None:
        """Record what the converger last carried out, which is what tells it it is done."""
        with self._lock:
            self._applied_by_key[_key_for(spec)] = _DesiredSync(spec=spec, activity=activity)

    def _restart(self, spec: FolderSyncSpec) -> None:
        """One step: replace a running sync's process, leaving its directories alone.

        What a change to direction or conflict needs. The copy on the machine is
        already where a sync wants it, so stopping properly -- which would rename
        it out of the way and back again -- would be two round trips and two
        renames to change a command-line flag.
        """
        with self._lock:
            run = self._runs_by_key.get(_key_for(spec))
        if run is not None and run.is_live():
            run.terminate()
        self._activate(spec, opening_state=FolderSyncState.RESTARTING)

    def _activate(self, spec: FolderSyncSpec, opening_state: FolderSyncState = FolderSyncState.STARTING) -> None:
        """One step: make room on the machine and spawn ``mngr pair``.

        ``opening_state`` is what the row says while the sync comes up. It is
        the one thing a restart does differently, and the difference is worth
        drawing: the files are already on the machine, so the user is waiting
        on a handshake rather than on a folder being copied across.
        """
        run = _FolderSyncRun(spec=spec, sink=_PairEventSink(state=opening_state))
        with self._lock:
            self._runs_by_key[_key_for(spec)] = run
        self._note_applied(spec, FolderSyncActivity.ACTIVE)
        self._bring_up(run)

    def _bring_up(self, run: _FolderSyncRun) -> None:
        """Make room on the machine, spawn ``mngr pair``, and set a watcher on it.

        Runs on the converger's thread, which is what keeps it off the click.
        Everything slow about starting a sync happens here, and anything that
        goes wrong settles the row as FAILED with the reason -- which is how the
        pane learns, since nobody is waiting on a return value.
        """
        try:
            # Behind the same lock as setting a copy aside: turning sync off
            # and straight back on is two renames of one directory.
            with self._workspace_lock_for(_key_for(run.spec)):
                workspace_target = self._prepare_workspace_target(run.spec)
            # The user may have changed their mind while the machine was being
            # asked; spawning now would leave a sync nobody wants running.
            if run.sink.is_stop_requested:
                run.sink.on_stopped()
                return
            run.process = self._spawn(run.spec, workspace_target, run.sink)
        except FolderSyncError as e:
            logger.warning("Could not start the folder sync for {}: {}", run.spec.local_path, e)
            run.sink.on_failed(str(e))
            return
        if run.sink.is_stop_requested:
            run.terminate()
            return
        self.concurrency_group.start_new_thread(
            target=run.watch,
            name=f"folder-sync-watcher-{run.spec.local_path}",
            daemon=True,
            is_checked=False,
        )

    def _set_copy_aside(self, spec: FolderSyncSpec) -> None:
        """One step: stop the sync if it runs, then move its copy out of the way."""
        with self._lock:
            run = self._runs_by_key.get(_key_for(spec))
        if run is not None and run.is_live():
            run.terminate()
        self._set_in_flight(_key_for(spec), FolderSyncState.DEACTIVATING)
        try:
            with self._workspace_lock_for(_key_for(spec)):
                self._run_workspace_script(
                    _build_workspace_deactivate_argv(self.mngr_binary, self._agent_address_for(spec), spec),
                    spec,
                    "set aside the copy of",
                )
            self._note_applied(spec, FolderSyncActivity.INACTIVE)
        except FolderSyncError as e:
            # Nothing to report to: the click that asked for this returned long
            # ago. Counted as applied anyway, so the converger retires instead
            # of making the same round trip 64 more times against a machine
            # that is not answering. The sync is off either way, which is what
            # the user asked for; only the copy is left where it was, and
            # turning sync on looks at the directories rather than at this, so
            # it is picked up rather than lost.
            logger.warning("Stopped syncing {} but could not set its copy aside: {}", spec.local_path, e)
            self._note_applied(spec, FolderSyncActivity.INACTIVE)
        finally:
            self._set_in_flight(_key_for(spec), None)

    def _discard_set_aside_copy(self, spec: FolderSyncSpec) -> None:
        """One step: delete a set-aside copy on the machine, for good."""
        self._set_in_flight(_key_for(spec), FolderSyncState.DISCARDING)
        try:
            with self._workspace_lock_for(_key_for(spec)):
                self._run_workspace_script(
                    _build_workspace_discard_argv(self.mngr_binary, self._agent_address_for(spec), spec),
                    spec,
                    "delete the copy of",
                )
        except FolderSyncError as e:
            logger.warning("Could not delete the set-aside copy of {}: {}", spec.local_path, e)
        finally:
            self._note_applied(spec, FolderSyncActivity.DISCARDED)
            self._set_in_flight(_key_for(spec), None)

    def _spec_for(
        self, agent_id: str, raw_local_path: str, direction: FolderSyncDirection, conflict: FolderSyncConflict
    ) -> FolderSyncSpec:
        """The spec one click describes, or the reason it cannot be built."""
        host_id = self._host_id_for(agent_id)
        if host_id is None:
            raise FolderSyncError(
                "Minds does not know which machine this workspace runs on yet, so it cannot sync with it. "
                "Try again in a moment."
            )
        return _build_folder_sync_spec(
            agent_id=agent_id,
            host_id=host_id,
            raw_local_path=raw_local_path,
            direction=direction,
            conflict=conflict,
            home_dir=self.home_dir,
            device_id=self.device_id,
        )

    def restore_failure_for(self, agent_id: str, local_path: str) -> str:
        """Why this sync was not brought back at launch, or empty when it was.

        Cleared as soon as anything moves the folder, so it describes only a
        sync still sitting where the failed restore left it.
        """
        with self._lock:
            return self._restore_failures.get(_SyncKey(agent_id=agent_id, local_path=local_path), "")

    def _forget_restore_failure(self, key: _SyncKey) -> None:
        """Drop a launch failure, for a folder something is about to move."""
        with self._lock:
            self._restore_failures.pop(key, None)

    def _known_spec_for(self, key: _SyncKey) -> FolderSyncSpec | None:
        """The settings a sync was last asked for, from wherever they are still held."""
        with self._lock:
            desired = self._desired_by_key.get(key)
            run = self._runs_by_key.get(key)
        if desired is not None:
            return desired.spec
        return None if run is None else run.spec

    def wait_until_settled(self, agent_id: str, local_path: str, timeout: float) -> bool:
        """Block until nothing is moving this sync, and say whether it got there.

        For a caller that has to see the end of a move rather than watch the
        row -- the tests, and anything that has to act on the outcome.
        Everything else polls the row.
        """
        key = _SyncKey(agent_id=agent_id, local_path=local_path)
        with self._lock:
            if key not in self._converging:
                return True
            settled = self._settled_events.get(key)
        return True if settled is None else settled.wait(timeout)

    def desired_conflict_for(self, agent_id: str, local_path: str) -> FolderSyncConflict:
        """The clash rule this sync was last asked for, for a caller changing something else."""
        with self._lock:
            desired = self._desired_by_key.get(_SyncKey(agent_id=agent_id, local_path=local_path))
        return FolderSyncConflict.NEWER if desired is None else desired.spec.conflict

    def desired_activity_for(self, agent_id: str, local_path: str) -> FolderSyncActivity | None:
        """Where the user last asked this sync to end up, or None when never asked."""
        with self._lock:
            desired = self._desired_by_key.get(_SyncKey(agent_id=agent_id, local_path=local_path))
        return None if desired is None else desired.activity

    def remembered_for_agent(self, agent_id: str) -> tuple[FolderSyncRecord, ...]:
        """Every sync remembered for one workspace, running or not.

        What the pane draws a row's sync half from when nothing is running: a
        folder whose sync is off still has a copy on the machine to say
        something about, and settings to resume from.
        """
        return () if self.store is None else self.store.list_for_agent(agent_id)

    def _workspace_lock_for(self, key: _SyncKey) -> threading.Lock:
        """The lock serializing one sync's workspace-side moves."""
        with self._lock:
            return self._workspace_locks.setdefault(key, threading.Lock())

    def _set_in_flight(self, key: _SyncKey, state: FolderSyncState | None) -> None:
        """Note that a sync is (or is no longer) in the middle of a move."""
        with self._lock:
            if state is None:
                self._in_flight_by_key.pop(key, None)
            else:
                self._in_flight_by_key[key] = state

    def in_flight_state_for(self, agent_id: str, local_path: str) -> FolderSyncState | None:
        """What this sync is in the middle of, or None when it is settled.

        The row shows this over anything else, because it is the most recent
        true thing: the user's destination is already recorded, and this says
        how far away from it the folder still is.
        """
        with self._lock:
            return self._in_flight_by_key.get(_SyncKey(agent_id=agent_id, local_path=local_path))

    def _record_for(self, agent_id: str, local_path: str) -> FolderSyncRecord | None:
        """The remembered sync for one shared path, or None when there is none."""
        if self.store is None:
            return None
        return next(
            (record for record in self.store.list_for_agent(agent_id) if record.local_path == local_path), None
        )

    def _spec_for_record(self, record: FolderSyncRecord) -> FolderSyncSpec:
        """Rebuild the spec a remembered sync was started from, to act on its directories again."""
        host_id = self._host_id_for(record.agent_id)
        if host_id is None:
            raise FolderSyncError(
                "Minds does not know which machine this workspace runs on yet. Try again in a moment."
            )
        return _build_folder_sync_spec(
            agent_id=record.agent_id,
            host_id=host_id,
            raw_local_path=record.local_path,
            direction=record.direction,
            conflict=record.conflict,
            home_dir=self.home_dir,
            device_id=self.device_id,
        )

    def _run_workspace_script(self, argv: list[str], spec: FolderSyncSpec, description: str) -> str:
        """Run one workspace-side script and return the outcome word it printed.

        Raises :class:`FolderSyncError` when the machine cannot be reached or
        the script failed, naming what was being attempted.
        """
        try:
            with log_span("Running a folder-sync script on the workspace for {}", spec.local_path):
                stdout = run_mngr_to_completion(
                    self.concurrency_group,
                    argv,
                    self._subprocess_env(),
                    timeout_seconds=_WORKSPACE_MKDIR_TIMEOUT_SECONDS,
                )
        except MngrCommandError as e:
            raise FolderSyncError(f"Could not reach the workspace to {description} {spec.local_path}: {e}") from e
        answer = extract_exec_stdout(stdout)
        if answer is None:
            detail = extract_exec_failure_detail(stdout) or "the workspace did not say why"
            raise FolderSyncError(f"Could not {description} {spec.local_path}: {detail}")
        _prefix, _, outcome = answer.strip().partition(_WORKSPACE_OUTCOME_PREFIX)
        return outcome

    def forget_shared_path(self, agent_id: str, local_path: str) -> None:
        """Stop syncing a path that is no longer shared, and delete the machine's copy.

        Returns at once, like every other move here: the deletion is a
        destination the converger walks to, stopping a running sync on the way.

        The copy goes because nothing would be left to offer it from. The pane
        draws its rows from the shared paths, so a copy whose path is no longer
        shared has no row, no Delete button, and nothing that would ever
        mention it again -- it would simply sit on the machine's disk. The sync
        option says so before the user gets here.
        """
        spec = self._known_spec_for(_SyncKey(agent_id=agent_id, local_path=local_path)) or self._spec_for_forgotten(
            agent_id, local_path
        )
        if spec is not None:
            self._set_desired(spec, FolderSyncActivity.DISCARDED)
        self.forget(agent_id, local_path)
        if self.store is not None:
            self.store.forget(agent_id, local_path)

    def _spec_for_forgotten(self, agent_id: str, local_path: str) -> FolderSyncSpec | None:
        """The spec of a remembered sync nothing is holding in memory, if there is one.

        What a sync stopped in an earlier run of Minds leaves behind: a record
        and a copy on the machine, and no reason to have been touched since.
        """
        record = self._record_for(agent_id, local_path)
        if record is None:
            return None
        try:
            return self._spec_for_record(record)
        except FolderSyncError as e:
            logger.warning("Could not work out where the copy of {} is, so it is left in place: {}", local_path, e)
            return None

    def forget(self, agent_id: str, local_path: str) -> None:
        """Drop a finished sync from the in-memory list. A live one is left alone."""
        key = _SyncKey(agent_id=agent_id, local_path=local_path)
        with self._lock:
            run = self._runs_by_key.get(key)
            if run is not None and not run.is_live():
                del self._runs_by_key[key]

    def wait_until_started(self, agent_id: str, local_path: str, timeout: float) -> FolderSyncStatus | None:
        """Block until a sync has finished starting, then report where it got to.

        Bringing a sync up is asynchronous -- see :meth:`start` -- so this is
        for a caller that would rather know the outcome than watch the row.
        Returns None when no such sync is known.
        """
        with self._lock:
            run = self._runs_by_key.get(_SyncKey(agent_id=agent_id, local_path=local_path))
        if run is None:
            return None
        run.sink.wait_until_started(timeout)
        return run.to_status()

    def status_for_path(self, agent_id: str, local_path: str) -> FolderSyncStatus | None:
        """One workspace's sync of one shared path, or None when it is not synced."""
        with self._lock:
            run = self._runs_by_key.get(_SyncKey(agent_id=agent_id, local_path=local_path))
        return None if run is None else run.to_status()

    def list_for_agent(self, agent_id: str) -> tuple[FolderSyncStatus, ...]:
        """Every sync belonging to one workspace, in the order they were started."""
        with self._lock:
            runs = tuple(run for run in self._runs_by_key.values() if run.spec.agent_id == agent_id)
        return tuple(run.to_status() for run in runs)

    def _wait_until_host_is_known(self, agent_id: str) -> bool:
        """Block until discovery can say which machine ``agent_id`` runs on.

        Only at launch does this ever wait. Which machine a workspace runs on
        comes from the discovery pipeline's snapshot, which is empty until its
        first pass lands -- and :meth:`restore_all` starts with the app, so it
        used to ask before there was an answer and give up, every time.
        """
        host_id, _polls, _elapsed = poll_for_value(
            lambda: self._host_id_for(agent_id),
            timeout=self.restore_host_wait_seconds,
            poll_interval=_RESTORE_HOST_POLL_SECONDS,
        )
        return host_id is not None

    def _agent_address_for(self, spec: FolderSyncSpec) -> str:
        """How ``mngr`` reaches the workspace a sync is with (see ``build_agent_address``)."""
        return build_agent_address(AgentId(spec.agent_id), self.backend_resolver)

    def _host_id_for(self, agent_id: str) -> str | None:
        """The machine ``agent_id`` runs on, or None while Minds cannot say.

        None rather than a guess: a sync started against the wrong machine
        would write one desktop's files into somebody else's.
        """
        try:
            parsed = AgentId(agent_id)
        except ValueError:
            return None
        info = self.backend_resolver.get_agent_display_info(parsed)
        return info.host_id if info is not None and info.host_id else None

    def _remember(self, spec: FolderSyncSpec, activity: FolderSyncActivity) -> None:
        """Record what has become of one sync, or say why it could not be.

        A failure here is logged rather than raised: whatever the user asked
        for has already happened on the machine, and refusing the click over a
        record that only matters at the next launch would be the worse answer.
        The cost is that the record can fall behind the machine, which the
        directory checks on either side of every move are what recover from.
        """
        if self.store is None:
            return
        try:
            self.store.remember(
                FolderSyncRecord(
                    agent_id=spec.agent_id,
                    local_path=spec.local_path,
                    direction=spec.direction,
                    conflict=spec.conflict,
                    device_id=self.device_id,
                    activity=activity,
                )
            )
        except FolderSyncStoreError as e:
            logger.warning("Could not remember the state of the sync of {}: {}", spec.local_path, e)

    def restore_all(self) -> None:
        """Start every sync this computer remembered, at launch.

        The point of keeping a copy on the machine is that it is there when
        Minds is not, so the copy has to catch up as soon as Minds is back
        rather than when someone next opens the panel.

        Every failure is per-folder and lands on that folder's row: a machine
        that is stopped (nothing here starts one), a workspace Minds cannot
        place yet, or a folder that is no longer on this computer -- an
        unmounted disk, or one the user deleted. None of them are reasons to
        abandon the other folders, and none of them forget the intent, which is
        still what the user asked for and may hold again tomorrow.

        The machine's copy may also be gone, since an agent owns its own
        filesystem: starting a sync makes the directory again if it has to, so
        a deleted copy costs a re-fetch rather than a failure.
        """
        if self.store is None:
            return
        for record in self.store.list_all():
            # An inactive sync is remembered so its copy and settings survive,
            # not so it comes back on its own; only the user turns one on.
            if record.activity != FolderSyncActivity.ACTIVE:
                continue
            if not self._wait_until_host_is_known(record.agent_id):
                message = (
                    "Minds could not work out which machine this workspace runs on, so it did not "
                    "start syncing. Try again once the workspace is listed."
                )
                logger.warning("Could not restore the sync of {}: {}", record.local_path, message)
                key = _SyncKey(agent_id=record.agent_id, local_path=record.local_path)
                with self._lock:
                    self._restore_failures[key] = message
                continue
            try:
                self.start(
                    agent_id=record.agent_id,
                    raw_local_path=record.local_path,
                    direction=record.direction,
                    conflict=record.conflict,
                )
            except FolderSyncError as e:
                logger.warning("Could not restore the sync of {}: {}", record.local_path, e)
                key = _SyncKey(agent_id=record.agent_id, local_path=record.local_path)
                with self._lock:
                    self._restore_failures[key] = str(e)

    def stop_all(self) -> None:
        """Stop every sync, so no unison outlives the app. Called on shutdown."""
        with self._lock:
            runs = tuple(self._runs_by_key.values())
        for run in runs:
            run.terminate()

    def _prepare_workspace_target(self, spec: FolderSyncSpec) -> "_WorkspaceTarget":
        """Make room for the sync in the workspace and return where it landed.

        ``mngr pair`` refuses to start on a directory that is not there, so this
        runs first rather than leaving the user to go and make it. It comes back
        with the absolute path, which the caller hands to ``--source-path``.
        """
        argv = _build_workspace_prepare_argv(self.mngr_binary, self._agent_address_for(spec), spec)
        try:
            with log_span("Preparing {} in the workspace", spec.workspace_path):
                stdout = run_mngr_to_completion(
                    self.concurrency_group,
                    argv,
                    self._subprocess_env(),
                    timeout_seconds=_WORKSPACE_MKDIR_TIMEOUT_SECONDS,
                )
        except MngrCommandError as e:
            raise FolderSyncError(f"Could not reach the workspace to prepare {spec.workspace_path}: {e}") from e
        answer = extract_exec_stdout(stdout)
        if answer is None:
            detail = extract_exec_failure_detail(stdout) or "the workspace did not say why"
            raise FolderSyncError(f"Could not create {spec.workspace_path} in the workspace: {detail}")
        # The script says what it found before printing the home directory, so
        # the last line is the home and whatever precedes it is the outcome.
        lines = [line for line in answer.strip().splitlines() if line.strip()]
        home_dir = lines[-1].strip() if lines else ""
        if not home_dir.startswith("/"):
            detail = extract_exec_failure_detail(stdout) or "the workspace did not say where its home is"
            raise FolderSyncError(f"Could not create {spec.workspace_path} in the workspace: {detail}")
        is_resumed = any(line.strip() == f"{_WORKSPACE_OUTCOME_PREFIX}resumed" for line in lines)
        return _WorkspaceTarget(
            path=f"{home_dir.rstrip('/')}/{WORKSPACE_SYNC_DIRECTORY}/{spec.workspace_path}",
            is_resumed=is_resumed,
        )

    def _subprocess_env(self) -> dict[str, str]:
        """The environment every ``mngr`` this manager runs inherits."""
        env = dict(os.environ)
        env["MNGR_HOST_DIR"] = str(self.mngr_host_dir)
        return env

    def _spawn(self, spec: FolderSyncSpec, workspace_target: _WorkspaceTarget, sink: _PairEventSink) -> RunningProcess:
        argv = _build_pair_argv(self.mngr_binary, spec, workspace_target)
        logger.info("Starting folder sync: {}", " ".join(argv))
        try:
            process = self.concurrency_group.run_process_in_background(
                argv,
                env=self._subprocess_env(),
                cwd=self.home_dir,
                on_output=sink.on_output,
                # Stopping a sync signals the process, and a pair that dies on
                # its own is reported through the sync's own state -- neither
                # should surface as a concurrency-group failure.
                is_checked_by_group=False,
                # It streams for as long as the sync lasts; the sink keeps the
                # tail, so retaining the whole thing would only grow forever.
                is_output_accumulated=False,
            )
        except (OSError, ConcurrencyGroupError) as e:
            raise FolderSyncError(f"Could not start the sync: {e}") from e
        return process
