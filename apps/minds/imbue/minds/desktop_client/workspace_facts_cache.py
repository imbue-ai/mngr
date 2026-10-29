"""The install's cache of the per-workspace facts that live in each workspace (spec section 7.4).

The skipped update target, the update-row dismissals, and the backup-verification flag are
facts about one workspace every computer should agree on, so each lives in the workspace
itself: the first two beside ``data/.state/update-apply/schedule.json`` (written through the
update workspace program's ``read``), the flag in ``data/system/backup.toml`` (written through
``backup_verification_flag``). A stopped workspace can be neither read nor written, so this
cache keeps the last value read per workspace (one JSON file per workspace under the install),
serves it while the workspace is stopped, and queues changes as dirty halves until a
write-through lands.

Merging favors whichever side knows more: a dismissal covers its run and every older one, so
the later run's start wins; a skip carries when it was chosen, and the later choice wins. A
fact the workspace does not hold never clears a cached value -- absence is not evidence.
"""

import threading
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
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.update_status import parse_update_epoch
from imbue.minds.errors import WorkspaceFactsCacheError
from imbue.minds.workspace_programs.update_protocol import DISMISS_NOTE_AT_FLAG
from imbue.minds.workspace_programs.update_protocol import DISMISS_OUTCOME_AT_FLAG
from imbue.minds.workspace_programs.update_protocol import SKIP_TARGET_AT_FLAG
from imbue.minds.workspace_programs.update_protocol import SKIP_TARGET_FLAG
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.file_utils import atomic_write


class WorkspaceSkippedTarget(FrozenModel):
    """The release the user skipped for one workspace, as its skipped-target file holds it."""

    target_ref: str = Field(description="The skipped release; '' when none was ever skipped")
    skipped_at: datetime | None = Field(description="When the skip was chosen; the later choice wins across computers")


class WorkspaceUpdateDismissals(FrozenModel):
    """Which runs' note and outcome were dismissed for one workspace, as its dismissals file holds them."""

    note_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose 'Updated to X' note was dismissed"
    )
    outcome_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose outcome (verdict or stall) was dismissed"
    )


@pure
def parse_workspace_skipped_target(raw: object) -> WorkspaceSkippedTarget | None:
    """Parse the program's skipped-target answer; None when there is none or it is unreadable."""
    if not isinstance(raw, dict):
        return None
    record: dict[str, object] = {str(key): value for key, value in raw.items()}
    target_ref = record.get("target_ref")
    if not isinstance(target_ref, str) or not target_ref:
        return None
    return WorkspaceSkippedTarget(target_ref=target_ref, skipped_at=parse_update_epoch(record.get("skipped_at")))


@pure
def parse_workspace_dismissals(raw: object) -> WorkspaceUpdateDismissals | None:
    """Parse the program's dismissals answer; None when there is none or it is unreadable."""
    if not isinstance(raw, dict):
        return None
    record: dict[str, object] = {str(key): value for key, value in raw.items()}
    return WorkspaceUpdateDismissals(
        note_run_started_at=parse_update_epoch(record.get("note_run_started_at")),
        outcome_run_started_at=parse_update_epoch(record.get("outcome_run_started_at")),
    )


@pure
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


@pure
def _max_dismissal(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


class WorkspaceFactsRecord(FrozenModel):
    """One workspace's cached facts: the last values read from it, plus the queued changes not yet written through."""

    # A file written by a newer build must stay readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    agent_id: str = Field(description="Workspace the facts are about")
    skipped_target_ref: str = Field(default="", description="The release the user skipped; '' for none")
    skipped_at: datetime | None = Field(default=None, description="When the skip was chosen")
    is_skip_dirty: bool = Field(default=False, description="Whether the skip still has to reach the workspace")
    note_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose 'Updated to X' note was dismissed"
    )
    is_note_dirty: bool = Field(
        default=False, description="Whether the note dismissal still has to reach the workspace"
    )
    outcome_run_started_at: datetime | None = Field(
        default=None, description="Start of the run whose outcome (verdict or stall) was dismissed"
    )
    is_outcome_dirty: bool = Field(
        default=False, description="Whether the outcome dismissal still has to reach the workspace"
    )
    is_backup_verification_disabled: bool = Field(
        default=False, description="Whether the workspace's backup verification is turned off"
    )
    is_backup_verification_dirty: bool = Field(
        default=False, description="Whether the verification flag still has to reach the workspace"
    )


class WorkspaceFactsCache(MutableModel):
    """On-disk cache of per-workspace facts (one JSON file per workspace), read through an in-memory copy."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    records_dir: Path = Field(frozen=True, description="Directory holding one <agent_id>.json per workspace")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # None until the directory has been read once; loaded lazily so a cache can be built before its dir exists.
    _record_by_agent: dict[str, WorkspaceFactsRecord] | None = PrivateAttr(default=None)

    def _record_path(self, agent_id: AgentId) -> Path:
        return self.records_dir / f"{agent_id}.json"

    def _records_locked(self) -> dict[str, WorkspaceFactsRecord]:
        """The in-memory records, read from the directory on first use. Must hold ``self._lock``."""
        if self._record_by_agent is None:
            self._record_by_agent = self._read_all_from_disk()
        return self._record_by_agent

    def _read_all_from_disk(self) -> dict[str, WorkspaceFactsRecord]:
        """Every record on disk, skipping unreadable files with a warning."""
        records: dict[str, WorkspaceFactsRecord] = {}
        if not self.records_dir.is_dir():
            return records
        for path in sorted(self.records_dir.glob("*.json")):
            try:
                agent_id = AgentId(path.stem)
            except ValueError:
                logger.warning("Ignoring workspace-facts file with a non-agent-id name: {}", path)
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("Could not read workspace-facts record {}: {}", path, e)
                continue
            try:
                record = WorkspaceFactsRecord.model_validate_json(raw)
            except ValueError as e:
                logger.warning("Workspace-facts record {} is not valid; ignoring it: {}", path, e)
                continue
            records[str(agent_id)] = record
        return records

    def load(self) -> None:
        """Read the directory in if it has not been read yet, so a later ``read`` is a lookup.

        Lets a caller pay the one directory glob before taking a lock of its own.
        """
        with self._lock:
            self._records_locked()

    def read(self, agent_id: AgentId) -> WorkspaceFactsRecord:
        """One workspace's cached facts (defaults when nothing was ever recorded)."""
        with self._lock:
            return self._record_or_empty_locked(agent_id)

    def get_skipped_target_ref(self, agent_id: AgentId) -> str:
        """The release the user skipped for this workspace, '' when they skipped none."""
        return self.read(agent_id).skipped_target_ref

    def is_backup_verification_enabled(self, agent_id: AgentId) -> bool:
        return not self.read(agent_id).is_backup_verification_disabled

    # Local writes (queued until a write-through lands)
    def dismiss_note(self, agent_id: AgentId, run_started_at: datetime) -> None:
        """Record that the note earned by the run starting at ``run_started_at`` was dismissed."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            merged_at = _max_dismissal(record.note_run_started_at, run_started_at)
            self._write_record_locked(
                record.model_copy_update(
                    to_update(record.field_ref().note_run_started_at, merged_at),
                    to_update(record.field_ref().is_note_dirty, True),
                )
            )

    def dismiss_outcome(self, agent_id: AgentId, run_started_at: datetime) -> None:
        """Record that the outcome of the run starting at ``run_started_at`` was dismissed."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            merged_at = _max_dismissal(record.outcome_run_started_at, run_started_at)
            self._write_record_locked(
                record.model_copy_update(
                    to_update(record.field_ref().outcome_run_started_at, merged_at),
                    to_update(record.field_ref().is_outcome_dirty, True),
                )
            )

    def record_skip(self, agent_id: AgentId, target_ref: str) -> None:
        """Record that the user skipped ``target_ref`` for this workspace, stamped now."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            self._write_record_locked(
                record.model_copy_update(
                    to_update(record.field_ref().skipped_target_ref, target_ref),
                    to_update(record.field_ref().skipped_at, datetime.now(timezone.utc)),
                    to_update(record.field_ref().is_skip_dirty, True),
                )
            )

    def set_backup_verification_enabled(self, agent_id: AgentId, is_enabled: bool) -> None:
        """Queue the verification flag; the caller attempts the write-through."""
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            self._write_record_locked(
                record.model_copy_update(
                    to_update(record.field_ref().is_backup_verification_disabled, not is_enabled),
                    to_update(record.field_ref().is_backup_verification_dirty, True),
                )
            )

    def list_agents_with_dirty_backup_verification(self) -> list[AgentId]:
        with self._lock:
            return [
                AgentId(aid_str)
                for aid_str, record in self._records_locked().items()
                if record.is_backup_verification_dirty
            ]

    # Write-through plumbing
    def pending_update_fact_arguments(self, agent_id: AgentId) -> list[str]:
        """The update ``read``'s write-through flags for this workspace's queued facts (empty when clean)."""
        record = self.read(agent_id)
        arguments: list[str] = []
        if record.is_skip_dirty and record.skipped_target_ref:
            arguments += [SKIP_TARGET_FLAG, record.skipped_target_ref]
            if record.skipped_at is not None:
                arguments += [SKIP_TARGET_AT_FLAG, str(record.skipped_at.timestamp())]
        if record.is_note_dirty and record.note_run_started_at is not None:
            arguments += [DISMISS_NOTE_AT_FLAG, str(record.note_run_started_at.timestamp())]
        if record.is_outcome_dirty and record.outcome_run_started_at is not None:
            arguments += [DISMISS_OUTCOME_AT_FLAG, str(record.outcome_run_started_at.timestamp())]
        return arguments

    def adopt_update_facts(
        self,
        agent_id: AgentId,
        skipped_target: WorkspaceSkippedTarget | None,
        dismissals: WorkspaceUpdateDismissals | None,
    ) -> None:
        """Fold what a workspace read answered into the cache; a queued half goes clean once the workspace covers it.

        A fact the workspace does not hold never clears a cached value: the read may predate
        this install's own write-through, and absence is not evidence of an un-skip or an
        un-dismissal (neither of which exists).
        """
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            updated = record
            if skipped_target is not None:
                is_workspace_current = record.skipped_at is None or (
                    skipped_target.skipped_at is not None and skipped_target.skipped_at >= record.skipped_at
                )
                if is_workspace_current:
                    updated = updated.model_copy_update(
                        to_update(updated.field_ref().skipped_target_ref, skipped_target.target_ref),
                        to_update(updated.field_ref().skipped_at, skipped_target.skipped_at),
                        to_update(updated.field_ref().is_skip_dirty, False),
                    )
            if dismissals is not None:
                merged_note = _max_dismissal(record.note_run_started_at, dismissals.note_run_started_at)
                merged_outcome = _max_dismissal(record.outcome_run_started_at, dismissals.outcome_run_started_at)
                is_note_covered = is_run_dismissed(dismissals.note_run_started_at, record.note_run_started_at)
                is_outcome_covered = is_run_dismissed(dismissals.outcome_run_started_at, record.outcome_run_started_at)
                updated = updated.model_copy_update(
                    to_update(updated.field_ref().note_run_started_at, merged_note),
                    to_update(updated.field_ref().is_note_dirty, updated.is_note_dirty and not is_note_covered),
                    to_update(updated.field_ref().outcome_run_started_at, merged_outcome),
                    to_update(
                        updated.field_ref().is_outcome_dirty, updated.is_outcome_dirty and not is_outcome_covered
                    ),
                )
            if updated != record:
                self._write_record_locked(updated)

    def adopt_backup_verification(
        self, agent_id: AgentId, *, is_disabled: bool, is_queued_write_applied: bool
    ) -> None:
        """Fold a workspace's verification flag into the cache.

        ``is_queued_write_applied`` says the read's answer already reflects this install's own
        queued write (a set that landed); without it, a still-dirty cache keeps its queued
        value over the workspace's, since the write-through has not happened yet. A landed
        write confirms only the value it carried: a record that moved since the write was read
        (the user toggled again while the exec was in flight) belongs to the newer toggle,
        whose own write-through settles it.
        """
        with self._lock:
            record = self._record_or_empty_locked(agent_id)
            if is_queued_write_applied:
                if record.is_backup_verification_disabled != is_disabled:
                    return
            elif record.is_backup_verification_dirty:
                return
            updated = record.model_copy_update(
                to_update(record.field_ref().is_backup_verification_disabled, is_disabled),
                to_update(record.field_ref().is_backup_verification_dirty, False),
            )
            if updated != record:
                self._write_record_locked(updated)

    # Migration seeding (CLEANUP: remove with workspace_facts_migration.py once every supported
    # install has started a phase-3B build.)
    def seed_from_legacy_stores_if_absent(
        self,
        agent_id: AgentId,
        *,
        skipped_target_ref: str,
        dismissals: WorkspaceUpdateDismissals | None,
        is_backup_verification_disabled: bool,
    ) -> bool:
        """Create a record from the pre-3B per-install stores, dirty so it is written through; False when one exists."""
        with self._lock:
            if str(agent_id) in self._records_locked():
                return False
            self._write_record_locked(
                WorkspaceFactsRecord(
                    agent_id=str(agent_id),
                    skipped_target_ref=skipped_target_ref,
                    skipped_at=datetime.now(timezone.utc) if skipped_target_ref else None,
                    is_skip_dirty=bool(skipped_target_ref),
                    note_run_started_at=dismissals.note_run_started_at if dismissals is not None else None,
                    is_note_dirty=dismissals is not None and dismissals.note_run_started_at is not None,
                    outcome_run_started_at=dismissals.outcome_run_started_at if dismissals is not None else None,
                    is_outcome_dirty=dismissals is not None and dismissals.outcome_run_started_at is not None,
                    is_backup_verification_disabled=is_backup_verification_disabled,
                    is_backup_verification_dirty=is_backup_verification_disabled,
                )
            )
            return True

    def _record_or_empty_locked(self, agent_id: AgentId) -> WorkspaceFactsRecord:
        record = self._records_locked().get(str(agent_id))
        return record if record is not None else WorkspaceFactsRecord(agent_id=str(agent_id))

    def _write_record_locked(self, record: WorkspaceFactsRecord) -> None:
        """Write one record to disk and memory. Must hold ``self._lock``."""
        path = self._record_path(AgentId(record.agent_id))
        try:
            atomic_write(path, record.model_dump_json(indent=2))
        except OSError as e:
            raise WorkspaceFactsCacheError(f"Could not write workspace-facts record {path}: {e}") from e
        self._records_locked()[record.agent_id] = record
