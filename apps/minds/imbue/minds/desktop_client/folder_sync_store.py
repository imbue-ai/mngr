"""Which folders the user asked to keep synced, remembered across restarts.

One atomically-written JSON file per workspace, mirroring
:mod:`update_schedule_store`, held in memory so the panel can be built without
touching the disk. What is recorded is only what the user chose -- the path,
which way changes move, and which side wins a conflict. Nothing about how a
sync is *going*: a sync's state belongs to the running ``mngr pair``, and a
persisted "SYNCING" would be a lie the moment Minds is not running.

The record outlives the sync. Turning sync off does not delete it, because the
machine is still holding the copy that sync produced: the record is what
remembers the copy is there, which settings to resume from, and -- once the
user throws the copy away -- that they did. Only unsharing the path drops the
record, since a path nobody shares is not one anybody can sync.

Three states, in :class:`FolderSyncActivity`: ACTIVE (running), INACTIVE (not
running, copy set aside on the machine), DISCARDED (not running, copy deleted).
The last two are Minds' belief rather than ground truth -- an agent owns its
own filesystem and may have deleted the copy itself -- so every code path that
acts on one tolerates finding the opposite.

Each record carries the device id of the computer that made it. Nothing reads
it yet -- a file on this computer is by definition this computer's -- but the
workspace side of a sync is already filed under it, and recording it here is
what will let these records move somewhere shared, where several desktops sync
with one workspace and only their own entries are theirs to run.
"""

import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncActivity
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncDirection
from imbue.minds.errors import FolderSyncStoreError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.file_utils import atomic_write


class FolderSyncRecord(FrozenModel):
    """One folder the user asked to keep synced, and how."""

    # A file written by a newer build must stay readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(description="Workspace this folder is synced with")
    local_path: str = Field(description="Absolute path of the folder on this computer")
    direction: FolderSyncDirection = Field(description="Which way changes move")
    conflict: FolderSyncConflict = Field(description="Which side wins a conflict (two-way syncs only)")
    device_id: str = Field(description="The computer that asked for this sync, and the one that runs it")
    activity: FolderSyncActivity = Field(
        default=FolderSyncActivity.ACTIVE,
        description="Whether this sync runs, and what became of the machine's copy when it stopped",
    )


class WorkspaceFolderSyncs(FrozenModel):
    """Every folder synced with one workspace: the contents of one file."""

    model_config = ConfigDict(extra="ignore")

    records: tuple[FolderSyncRecord, ...] = Field(default=(), description="The workspace's syncs, by local path")


OnFolderSyncsChangedCallback = Callable[[], None]


class FolderSyncStore(MutableModel):
    """On-disk record of which folders to keep synced, read through an in-memory copy."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    records_dir: Path = Field(frozen=True, description="Directory holding one <agent_id>.json per workspace")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until the directory has been read once; loaded lazily so a store can be built before its dir exists.
    _records_by_agent: dict[str, WorkspaceFolderSyncs] | None = PrivateAttr(default=None)
    _on_change_callbacks: list[OnFolderSyncsChangedCallback] = PrivateAttr(default_factory=list)

    def add_on_change_callback(self, callback: OnFolderSyncsChangedCallback) -> None:
        """Register a callback fired after any sync is remembered or forgotten."""
        with self._lock:
            self._on_change_callbacks.append(callback)

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("A folder-sync change callback failed: {}", e)

    def _record_path(self, agent_id: str) -> Path:
        return self.records_dir / f"{agent_id}.json"

    def _records_locked(self) -> dict[str, WorkspaceFolderSyncs]:
        """The in-memory records, read from the directory on first use. Must hold ``self._lock``."""
        if self._records_by_agent is None:
            self._records_by_agent = self._read_all_from_disk()
        return self._records_by_agent

    def _read_all_from_disk(self) -> dict[str, WorkspaceFolderSyncs]:
        """Every remembered sync on disk, skipping unreadable files with a warning."""
        records: dict[str, WorkspaceFolderSyncs] = {}
        if not self.records_dir.is_dir():
            return records
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                agent_id = AgentId(path.stem)
            except ValueError:
                logger.warning("Ignoring folder-sync file with a non-agent-id name: {}", path)
                continue
            syncs = self._read_from_disk(str(agent_id))
            if syncs is not None:
                records[str(agent_id)] = syncs
        return records

    def _read_from_disk(self, agent_id: str) -> WorkspaceFolderSyncs | None:
        path = self._record_path(agent_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read folder-sync record {}: {}", path, e)
            return None
        try:
            return WorkspaceFolderSyncs.model_validate_json(raw)
        except ValueError as e:
            logger.warning("Folder-sync record {} is not valid; ignoring it: {}", path, e)
            return None

    def _write_locked(self, agent_id: str, syncs: WorkspaceFolderSyncs) -> None:
        """Write one workspace's syncs to disk and memory. Must hold ``self._lock``.

        An empty set is deleted rather than written: an empty file and an absent
        one mean the same thing, and only one of them accumulates.
        """
        path = self._record_path(agent_id)
        if not syncs.records:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise FolderSyncStoreError(f"Could not delete folder-sync record {path}: {e}") from e
            self._records_locked().pop(agent_id, None)
            return
        try:
            atomic_write(path, syncs.model_dump_json(indent=2))
        except OSError as e:
            raise FolderSyncStoreError(f"Could not write folder-sync record {path}: {e}") from e
        self._records_locked()[agent_id] = syncs

    def remember(self, record: FolderSyncRecord) -> None:
        """Record that ``record.local_path`` should be kept synced, replacing any earlier settings for it.

        Raises :class:`FolderSyncStoreError` when the record cannot be written,
        which the caller reports rather than swallowing: a sync that is running
        but not remembered comes back as gone on the next launch.
        """
        with self._lock:
            existing = self._records_locked().get(record.agent_id, WorkspaceFolderSyncs())
            kept = tuple(other for other in existing.records if other.local_path != record.local_path)
            self._write_locked(record.agent_id, WorkspaceFolderSyncs(records=(*kept, record)))
        self._fire_on_change()

    def forget(self, agent_id: str, local_path: str) -> bool:
        """Stop recording ``local_path`` as synced. Returns whether it was. Idempotent."""
        with self._lock:
            existing = self._records_locked().get(agent_id)
            if existing is None:
                return False
            kept = tuple(other for other in existing.records if other.local_path != local_path)
            if len(kept) == len(existing.records):
                return False
            self._write_locked(agent_id, WorkspaceFolderSyncs(records=kept))
        self._fire_on_change()
        return True

    def forget_workspace(self, agent_id: str) -> None:
        """Drop every sync recorded for one workspace, for a workspace that is gone."""
        with self._lock:
            if self._records_locked().get(agent_id) is None:
                return
            self._write_locked(agent_id, WorkspaceFolderSyncs())
        self._fire_on_change()

    def list_for_agent(self, agent_id: str) -> tuple[FolderSyncRecord, ...]:
        """Every sync recorded for one workspace, in the order they were remembered."""
        with self._lock:
            existing = self._records_locked().get(agent_id)
            return () if existing is None else existing.records

    def list_all(self) -> tuple[FolderSyncRecord, ...]:
        """Every sync recorded on this computer, in workspace order."""
        with self._lock:
            records = self._records_locked()
            return tuple(record for agent_id in sorted(records) for record in records[agent_id].records)
