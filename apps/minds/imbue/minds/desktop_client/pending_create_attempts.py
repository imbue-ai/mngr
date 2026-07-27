"""Durable pending-create-attempt records for workspace creates.

A record is written *before* the ``mngr create`` subprocess is spawned and
deleted only after the finished workspace has been confirmed by a discovery
snapshot. That closes the association gap that used to orphan Lima/Docker VMs:
if the app quits or crashes mid-create, the record survives the restart and
the startup reconcile can adopt the finished host (re-associating it with the
account from the record) or destroy a stale half-built one.

Records are local-only (never synced): one JSON file per create attempt under
``<data_dir>/pending_create_attempts/``, mirroring the ``destroying/`` directory
convention. Each record carries the full create request so a retry can
pre-fill the create form, and -- once a create attempt fails -- the error plus the
tail of the create attempt log so the failure stays inspectable across restarts.

State machine: ``IN_FLIGHT`` (written at start; a record still IN_FLIGHT at
the next app startup means the create was interrupted) -> ``FAILED`` (the
create attempt thread reported an error) or ``DONE`` (the create returned canonical
ids; the record now only awaits discovery confirmation before deletion).
"""

import threading
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import PendingCreateAttemptStoreError
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import DockerRuntime
from imbue.minds.primitives import LaunchMode


class PendingCreateAttemptState(UpperCaseStrEnum):
    """Lifecycle state of a persisted pending-create-attempt record."""

    # The create is (or was, if the app died) running. A record still
    # IN_FLIGHT at startup with no live create attempt behind it is "interrupted".
    IN_FLIGHT = auto()
    # The create attempt thread reported a failure; the record carries the error
    # and the log tail so the failed row survives restarts until dismissed.
    FAILED = auto()
    # ``mngr create`` returned canonical ids; the record is deleted once the
    # workspace appears in a discovery snapshot (never on a timer).
    DONE = auto()


class PendingCreateAttemptRequest(FrozenModel):
    """The full create request, persisted so a retry can pre-fill the form."""

    # Like the record itself: ignore unknown fields so a file written by a
    # newer build stays readable after a downgrade.
    model_config = ConfigDict(extra="ignore")

    repo_source: str = Field(description="Git URL or local path the workspace is created from")
    host_name: str = Field(description="Resolved host-name slug (the per-provider-unique workspace name)")
    display_name: str = Field(default="", description="Human-readable workspace name (may differ from the slug)")
    branch: str = Field(default="", description="Requested branch/tag/SHA, empty for the repo default")
    launch_mode: LaunchMode = Field(description="Compute launch mode")
    account_id: str = Field(default="", description="Owning account's user id, empty for a private workspace")
    account_email: str = Field(default="", description="Owning account's email, empty for a private workspace")
    branch_or_tag: str = Field(default="", description="Resolved template version for imbue_cloud leases")
    region: str = Field(default="", description="Resolved region, empty for region-less providers")
    cloud_account: str = Field(default="", description="Bring-your-own-key account block name, if any")
    instance_type: str = Field(default="", description="Per-create machine size, if the mode has one")
    color: str | None = Field(default=None, description="Workspace accent color (#rrggbb), if chosen")
    docker_runtime: DockerRuntime = Field(default=DockerRuntime.RUNC, description="Container runtime for DOCKER mode")
    original_minds_version: str = Field(default="", description="Template ref stamped on the workspace at create")
    backup_provider: BackupProvider = Field(
        default=BackupProvider.CONFIGURE_LATER, description="Requested backup provider"
    )
    backup_api_key_env: str = Field(
        default="", description="User-supplied restic env block for API_KEY backups (local-only, like restic.env)"
    )


class PendingCreateAttemptRecord(FrozenModel):
    """One persisted pending-create-attempt, backing crash-safe adoption and retry."""

    # Unknown fields are ignored rather than rejected: records are read back
    # across app versions, and a file written by a newer build must not make
    # the whole record unreadable (that would break the reconcile exactly on
    # the first launch after a downgrade).
    model_config = ConfigDict(extra="ignore")

    create_attempt_id: str = Field(description="Opaque create attempt id; also the host's workspace-id label value")
    state: PendingCreateAttemptState = Field(description="Lifecycle state of this record")
    provider_instance_name: str = Field(
        default="", description="mngr provider instance the create targets (the host-name uniqueness scope)"
    )
    created_at: datetime = Field(description="When the create was started (UTC)")
    updated_at: datetime = Field(description="When this record last changed (UTC)")
    request: PendingCreateAttemptRequest = Field(description="The full create request, for retry pre-fill")
    agent_id: str | None = Field(default=None, description="Canonical agent id, once mngr create returned")
    host_id: str | None = Field(default=None, description="Canonical host id, once mngr create returned")
    error: str | None = Field(default=None, description="Error message, set when state is FAILED")
    error_kind: str | None = Field(
        default=None,
        description="Machine-readable failure classification (CreateAttemptErrorKind value), if recognized",
    )
    log_tail: tuple[str, ...] = Field(
        default=(), description="Last lines of the create attempt log, snapshotted when the create attempt failed"
    )


# How many trailing create-attempt-log lines a FAILED record retains, so the error's
# context is visible on the failed row across restarts.
FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES: Final[int] = 1000

# The mngr HOST label carrying the opaque pending-create-attempt id on Lima / Docker
# hosts. It is the only thing stamped on the host: account and display
# metadata stay in the local record. The startup reconcile joins hosts to
# records through this label.
WORKSPACE_ID_HOST_LABEL: Final[str] = "workspace-id"


class PendingCreateAttemptStore(MutableModel):
    """On-disk store of pending-create-attempt records (one JSON file per create attempt)."""

    records_dir: Path = Field(frozen=True, description="Directory holding one <create_attempt_id>.json per record")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    # CreateAttempt ids of records currently in the DONE state, kept in memory so
    # the discovery-confirmation sweep (which runs on every resolver change)
    # can no-op cheaply when nothing awaits confirmation. Seeded from disk.
    _done_create_attempt_ids: set[str] = PrivateAttr(default_factory=set)

    def model_post_init(self, __context: object) -> None:
        for record in self.list_records():
            if record.state is PendingCreateAttemptState.DONE:
                self._done_create_attempt_ids.add(record.create_attempt_id)

    def _record_path(self, create_attempt_id: str) -> Path:
        return self.records_dir / f"{create_attempt_id}.json"

    def write_record(self, record: PendingCreateAttemptRecord) -> None:
        """Persist ``record`` atomically (tmp file + rename).

        Raises ``PendingCreateAttemptStoreError`` on I/O failure so the caller can
        decide whether the create proceeds without crash-safety.
        """
        path = self._record_path(record.create_attempt_id)
        with self._lock:
            try:
                self.records_dir.mkdir(parents=True, exist_ok=True)
                tmp_path = path.with_suffix(".json.tmp")
                tmp_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
                tmp_path.replace(path)
            except OSError as e:
                raise PendingCreateAttemptStoreError(
                    f"Could not write pending-create-attempt record {path}: {e}"
                ) from e
            if record.state is PendingCreateAttemptState.DONE:
                self._done_create_attempt_ids.add(record.create_attempt_id)
            else:
                self._done_create_attempt_ids.discard(record.create_attempt_id)

    def read_record(self, create_attempt_id: str) -> PendingCreateAttemptRecord | None:
        """Read one record, or None when absent or unreadable (logged, never raised)."""
        path = self._record_path(create_attempt_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read pending-create-attempt record {}: {}", path, e)
            return None
        try:
            return PendingCreateAttemptRecord.model_validate_json(raw)
        except ValueError as e:
            logger.warning("Pending-create-attempt record {} is not valid; ignoring it: {}", path, e)
            return None

    def list_records(self) -> list[PendingCreateAttemptRecord]:
        """Read every record in the store, skipping unreadable files with a warning."""
        if not self.records_dir.is_dir():
            return []
        records: list[PendingCreateAttemptRecord] = []
        for path in sorted(self.records_dir.glob("*.json")):
            record = self.read_record(path.stem)
            if record is not None:
                records.append(record)
        return records

    def delete_record(self, create_attempt_id: str) -> bool:
        """Remove a record. Returns True when a file was deleted. Idempotent.

        The in-memory DONE-sweep id is only dropped once the file is actually
        gone: a transient unlink failure keeps the id so the discovery sweep
        retries the deletion on the next resolver change instead of losing
        track of the record until the next restart re-seeds the set from disk.
        """
        path = self._record_path(create_attempt_id)
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                self._done_create_attempt_ids.discard(create_attempt_id)
                return False
            except OSError as e:
                logger.warning("Could not delete pending-create-attempt record {}: {}", path, e)
                return False
            self._done_create_attempt_ids.discard(create_attempt_id)
        return True

    def mark_failed(
        self, create_attempt_id: str, error: str, error_kind: str | None, log_tail: tuple[str, ...]
    ) -> None:
        """Flip a record to FAILED, snapshotting the error and the create-attempt-log tail."""
        record = self.read_record(create_attempt_id)
        if record is None:
            logger.warning("No pending-create-attempt record to mark FAILED for {}", create_attempt_id)
            return
        updated = record.model_copy_update(
            to_update(record.field_ref().state, PendingCreateAttemptState.FAILED),
            to_update(record.field_ref().error, error),
            to_update(record.field_ref().error_kind, error_kind),
            to_update(record.field_ref().log_tail, log_tail[-FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES:]),
            to_update(record.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        self.write_record(updated)

    def mark_done(self, create_attempt_id: str, agent_id: str, host_id: str) -> None:
        """Flip a record to DONE with its canonical ids; deletion then awaits discovery confirmation."""
        record = self.read_record(create_attempt_id)
        if record is None:
            logger.warning("No pending-create-attempt record to mark DONE for {}", create_attempt_id)
            return
        updated = record.model_copy_update(
            to_update(record.field_ref().state, PendingCreateAttemptState.DONE),
            to_update(record.field_ref().agent_id, agent_id),
            to_update(record.field_ref().host_id, host_id),
            to_update(record.field_ref().updated_at, datetime.now(timezone.utc)),
        )
        self.write_record(updated)

    def has_done_records(self) -> bool:
        """Cheap gate for the discovery sweep: whether any record awaits confirmation."""
        with self._lock:
            return bool(self._done_create_attempt_ids)

    def sweep_confirmed_records(self, known_agent_id_strs: frozenset[str]) -> list[str]:
        """Delete DONE records whose workspace a discovery snapshot has confirmed.

        This is the only success-path deletion: a record survives until its
        canonical agent id shows up in discovery (no timeout bound), so the
        create attempt row can hand off to the real workspace row without flicker.
        Returns the deleted create attempt ids.
        """
        with self._lock:
            done_ids = tuple(self._done_create_attempt_ids)
        deleted: list[str] = []
        for create_attempt_id in done_ids:
            record = self.read_record(create_attempt_id)
            if record is None:
                with self._lock:
                    self._done_create_attempt_ids.discard(create_attempt_id)
                continue
            if record.agent_id is not None and record.agent_id in known_agent_id_strs:
                if self.delete_record(create_attempt_id):
                    logger.debug(
                        "Deleted pending-create-attempt record {} (workspace {} confirmed)",
                        create_attempt_id,
                        record.agent_id,
                    )
                    deleted.append(create_attempt_id)
        return deleted
