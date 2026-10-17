"""Durable scheduled-update intents: one atomically-written JSON file per workspace, mirroring ``pending_create_attempts``.

The records are also held in memory, so the update state store can compose a
row from them on every read without touching the disk, and every change is
announced so the row refreshes.
"""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import UpdateScheduleStoreError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.file_utils import atomic_write


class UpdateScheduleRecord(FrozenModel):
    """One workspace's armed scheduled update."""

    # A file written by a newer build must stay readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(description="Workspace the intent is for")
    created_at: datetime = Field(description="When the intent was recorded (UTC)")
    target_ref: str = Field(default="", description="The exact ref the run should target, '' for the skill's default")
    last_skip_reason: str = Field(default="", description="Why the most recent attempt did not run")
    last_skip_at: datetime | None = Field(default=None, description="When that skip happened (UTC)")


OnScheduleChangedCallback = Callable[[], None]


class UpdateScheduleStore(MutableModel):
    """On-disk store of scheduled-update intents (one JSON file per workspace), read through an in-memory copy."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    records_dir: Path = Field(frozen=True, description="Directory holding one <agent_id>.json per intent")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until the directory has been read once; loaded lazily so a store can be built before its dir exists.
    _record_by_agent: dict[str, UpdateScheduleRecord] | None = PrivateAttr(default=None)
    _on_change_callbacks: list[OnScheduleChangedCallback] = PrivateAttr(default_factory=list)

    def add_on_change_callback(self, callback: OnScheduleChangedCallback) -> None:
        """Register a callback fired after any intent is armed, skipped, or disarmed."""
        with self._lock:
            self._on_change_callbacks.append(callback)

    def _fire_on_change(self) -> None:
        with self._lock:
            callbacks = list(self._on_change_callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("An update-schedule change callback failed: {}", e)

    def _record_path(self, agent_id: AgentId) -> Path:
        return self.records_dir / f"{agent_id}.json"

    def _records_locked(self) -> dict[str, UpdateScheduleRecord]:
        """The in-memory records, read from the directory on first use. Must hold ``self._lock``."""
        if self._record_by_agent is None:
            self._record_by_agent = self._read_all_from_disk()
        return self._record_by_agent

    def _read_all_from_disk(self) -> dict[str, UpdateScheduleRecord]:
        """Every armed intent on disk, skipping unreadable files with a warning."""
        records: dict[str, UpdateScheduleRecord] = {}
        if not self.records_dir.is_dir():
            return records
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                agent_id = AgentId(path.stem)
            except ValueError:
                logger.warning("Ignoring update-schedule file with a non-agent-id name: {}", path)
                continue
            record = self._read_from_disk(agent_id)
            if record is not None:
                records[str(agent_id)] = record
        return records

    def _read_from_disk(self, agent_id: AgentId) -> UpdateScheduleRecord | None:
        path = self._record_path(agent_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read update-schedule record {}: {}", path, e)
            return None
        try:
            return UpdateScheduleRecord.model_validate_json(raw)
        except ValueError as e:
            logger.warning("Update-schedule record {} is not valid; ignoring it: {}", path, e)
            return None

    def _write_record_locked(self, record: UpdateScheduleRecord) -> None:
        """Write one record to disk and memory. Must hold ``self._lock``."""
        path = self._record_path(AgentId(record.agent_id))
        try:
            atomic_write(path, record.model_dump_json(indent=2))
        except OSError as e:
            raise UpdateScheduleStoreError(f"Could not write update-schedule record {path}: {e}") from e
        self._records_locked()[record.agent_id] = record

    def schedule(self, agent_id: AgentId, *, target_ref: str = "") -> UpdateScheduleRecord:
        """Arm (or re-arm) a scheduled update for ``agent_id``.

        Re-arming replaces the record outright: the old skip reason describes a run no longer scheduled.
        """
        record = UpdateScheduleRecord(
            agent_id=str(agent_id),
            created_at=datetime.now(timezone.utc),
            target_ref=target_ref,
        )
        with self._lock:
            self._write_record_locked(record)
        self._fire_on_change()
        return record

    def read(self, agent_id: AgentId) -> UpdateScheduleRecord | None:
        """Read one intent, or None when absent."""
        with self._lock:
            return self._records_locked().get(str(agent_id))

    def list_records(self) -> list[UpdateScheduleRecord]:
        """Every armed intent, in agent-id order."""
        with self._lock:
            records = self._records_locked()
            return [records[agent_id_str] for agent_id_str in sorted(records)]

    def cancel(self, agent_id: AgentId) -> bool:
        """Disarm ``agent_id``'s intent. Returns whether one was there. Idempotent."""
        path = self._record_path(agent_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                # The file would re-arm the intent at the next launch, so the memory copy stays with it.
                logger.warning("Could not delete update-schedule record {}: {}", path, e)
                return False
            was_armed = self._records_locked().pop(str(agent_id), None) is not None
        if was_armed:
            self._fire_on_change()
        return was_armed

    def record_skip(self, agent_id: AgentId, reason: str) -> None:
        """Note why the last attempt did not run, leaving the intent armed for the next window.

        Read and write share one lock hold: a cancel landing between them would re-create the intent the user just disarmed.
        """
        with self._lock:
            record = self._records_locked().get(str(agent_id))
            if record is None:
                return
            self._write_record_locked(
                record.model_copy_update(
                    to_update(record.field_ref().last_skip_reason, reason),
                    to_update(record.field_ref().last_skip_at, datetime.now(timezone.utc)),
                )
            )
        self._fire_on_change()
