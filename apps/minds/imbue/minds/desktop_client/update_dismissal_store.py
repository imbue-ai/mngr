"""Durable update-row dismissals: one atomically-written JSON file per workspace, mirroring ``update_schedule_store``.

A run's record stays in the workspace and is re-read by every launch's sweep, so a
dismissal has to outlive the app or the note and badge come back on each relaunch.
Each dismissal is remembered as the start of the run it was about: that run and any
older one stay dismissed, a newer run is news again.

The update state store folds these into a workspace's facts as it writes them, rather
than composing them on read as it does the armed schedule: a dismissed stall's record
is verdictless, and letting it through would put the row back into RUNNING, which is
what the apply window's recovery guard reads.
"""

import threading
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import UpdateDismissalStoreError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.file_utils import atomic_write


class UpdateDismissalRecord(FrozenModel):
    """What the user has dismissed on one workspace's update row."""

    # A file written by a newer build must stay readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(description="Workspace the dismissals are for")
    note_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose 'Updated to X' note was dismissed"
    )
    outcome_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose outcome (verdict or stall) was dismissed"
    )


def is_run_dismissed(dismissed_run_started_at: datetime | None, run_started_at: datetime | None) -> bool:
    """Whether the run starting at ``run_started_at`` is covered by a dismissal recorded at ``dismissed_run_started_at``.

    The two sides can come from different clocks. A run the app dispatched is stamped
    from this machine until the poll reads the workspace's own record, so a run
    dismissed before that read (a stall over an unreadable record) is keyed to the
    dispatch rather than to the start the next launch will read.
    """
    if dismissed_run_started_at is None:
        return False
    return run_started_at is None or run_started_at <= dismissed_run_started_at


class UpdateDismissalStore(MutableModel):
    """On-disk store of update-row dismissals (one JSON file per workspace), read through an in-memory copy."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    records_dir: Path = Field(frozen=True, description="Directory holding one <agent_id>.json per workspace")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until the directory has been read once; loaded lazily so a store can be built before its dir exists.
    _record_by_agent: dict[str, UpdateDismissalRecord] | None = PrivateAttr(default=None)

    def _record_path(self, agent_id: AgentId) -> Path:
        return self.records_dir / f"{agent_id}.json"

    def _records_locked(self) -> dict[str, UpdateDismissalRecord]:
        """The in-memory records, read from the directory on first use. Must hold ``self._lock``."""
        if self._record_by_agent is None:
            self._record_by_agent = self._read_all_from_disk()
        return self._record_by_agent

    def _read_all_from_disk(self) -> dict[str, UpdateDismissalRecord]:
        """Every record on disk, skipping unreadable files with a warning."""
        records: dict[str, UpdateDismissalRecord] = {}
        if not self.records_dir.is_dir():
            return records
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                agent_id = AgentId(path.stem)
            except ValueError:
                logger.warning("Ignoring update-dismissal file with a non-agent-id name: {}", path)
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Could not read update-dismissal record {}: {}", path, e)
                continue
            try:
                record = UpdateDismissalRecord.model_validate_json(raw)
            except ValueError as e:
                logger.warning("Update-dismissal record {} is not valid; ignoring it: {}", path, e)
                continue
            records[str(agent_id)] = record
        return records

    def load(self) -> None:
        """Read the directory in if it has not been read yet, so a later ``read`` is a lookup.

        Lets a caller pay the one directory glob before taking a lock of its own.
        """
        with self._lock:
            self._records_locked()

    def read(self, agent_id: AgentId) -> UpdateDismissalRecord:
        """One workspace's dismissals (none recorded when absent)."""
        with self._lock:
            return self._record_or_empty_locked(agent_id)

    def dismiss_note(self, agent_id: AgentId, run_started_at: datetime) -> None:
        """Record that the note earned by the run starting at ``run_started_at`` was dismissed."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            self._write_record_locked(
                record.model_copy_update(to_update(record.field_ref().note_run_started_at, run_started_at))
            )

    def dismiss_outcome(self, agent_id: AgentId, run_started_at: datetime) -> None:
        """Record that the outcome of the run starting at ``run_started_at`` was dismissed."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            self._write_record_locked(
                record.model_copy_update(to_update(record.field_ref().outcome_run_started_at, run_started_at))
            )

    def _record_or_empty_locked(self, agent_id: AgentId) -> UpdateDismissalRecord:
        record = self._records_locked().get(str(agent_id))
        return record if record is not None else UpdateDismissalRecord(agent_id=str(agent_id))

    def _write_record_locked(self, record: UpdateDismissalRecord) -> None:
        """Write one record to disk and memory. Must hold ``self._lock``."""
        path = self._record_path(AgentId(record.agent_id))
        try:
            atomic_write(path, record.model_dump_json(indent=2))
        except OSError as e:
            raise UpdateDismissalStoreError(f"Could not write update-dismissal record {path}: {e}") from e
        self._records_locked()[record.agent_id] = record
