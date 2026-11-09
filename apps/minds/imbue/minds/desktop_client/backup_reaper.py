"""Client-side reaper for destroyed workspaces' backups, plus quota-pressure eviction.

Destroyed workspaces keep their backup (backup-store repository or legacy R2
bucket + workspace record + local ``restic.env``) for a retention window
(served by the connector, 30 days) and are then reaped: repository first
(deleted server-side through the CLI), then the record, then the local env.
The reaper runs on its own cadence inside the sync scheduler's reconcile
loop, on a background thread under the app's concurrency group, guarded by a
lock so only one pass is ever in flight. The connector runs a server-side
backstop hourly; every step here is idempotent so the two never conflict.

Quota-pressure eviction reuses the same candidate set: when backup
provisioning hits a repository-count or storage quota, the oldest destroyed
workspace's backup -- something that would age out anyway -- is evicted and
provisioning retries.

BYO backup backends (the user's own S3) are never reaped at the repository
level: only the record and local env are cleaned up.
"""

import json
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from enum import auto
from typing import Final
from typing import assert_never
from uuid import uuid4

import httpx2
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import backup_env_dir
from imbue.minds.desktop_client.backup_env_store import backup_store_repository_from_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import remove_canonical_env_and_archives
from imbue.minds.desktop_client.backup_env_store import split_backup_bucket_name_from_env
from imbue.minds.desktop_client.backup_export import is_export_in_flight
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_DESTROYED
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.primitives import AgentId

# Fallback when the connector's policy endpoint is unreachable (or the
# connector predates it). Must match the connector's
# DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS.
BACKUP_RETENTION_FALLBACK_SECONDS: Final[float] = 60.0 * 60.0 * 24.0 * 30.0

# The reaper's own cadence inside the (60s) reconcile loop: every 30 minutes,
# with a first pass shortly after startup so short-lived sessions still reap.
_REAP_INTERVAL_SECONDS: Final[float] = 30.0 * 60.0
_REAP_STARTUP_DELAY_SECONDS: Final[float] = 120.0
# At most this many reaps per pass, so a large post-rollout backlog drains
# over a few passes without monopolizing the connector or the CLI.
_REAP_CAP_PER_PASS: Final[int] = 5

_RETENTION_POLICY_PATH: Final[str] = "/policies/destroyed-workspace-backups"
_RETENTION_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
_RETENTION_CACHE_TTL_SECONDS: Final[float] = 60.0 * 60.0

# Events land under events/<source>/events.jsonl in the Imbue Studio data dir.
_EVENT_SOURCE: Final[str] = "backup_reaper"

# The plugin CLI writes the raising exception's class name into its stderr JSON
# (``error_class``), and the class it raises for a bucket the connector no
# longer has is its own ``ImbueCloudBucketNotFoundError``, never the connector's
# ``R2BucketNotFoundError``. A missing bucket during a reap just means someone
# else got there first.
_BUCKET_NOT_FOUND_SIGNAL: Final[str] = "ImbueCloudBucketNotFoundError"


class ReapCandidate(FrozenModel):
    """One destroyed workspace whose backup is eligible for reaping/eviction."""

    user_id: str = Field(description="Owning account's user id")
    account_email: str = Field(description="Owning account's email (CLI --account)")
    agent_id: str = Field(description="Workspace id (keys the record and the local env file)")
    host_id: str = Field(description="The workspace's machine id (names legacy backup buckets)")
    backup_bucket: str | None = Field(
        default=None,
        description="The record's backup location, when known: a store repository path or a legacy bucket name",
    )
    display_name: str = Field(description="Workspace display name (for logs/events)")
    destroyed_at: datetime = Field(description="When the workspace was tombstoned")


@pure
def parse_destroyed_at(value: str | None) -> datetime | None:
    """Parse a record's destroyed_at wire string into an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class BackupTargetKind(UpperCaseStrEnum):
    """Where a workspace's imbue_cloud backup lives."""

    # A repository in the per-environment backup store (restic REST).
    STORE_REPOSITORY = auto()
    # A per-workspace R2 bucket from before the backup store, read-only until
    # the migration's legacy reap.
    # CLEANUP: remove with the legacy bucket surface once that reap has run on production.
    LEGACY_BUCKET = auto()


class BackupTarget(FrozenModel):
    """One reapable imbue_cloud backup: a store repository (by workspace id) or a legacy bucket (by short name)."""

    kind: BackupTargetKind = Field(description="Store repository or legacy bucket")
    name: str = Field(description="The workspace id (store) or the bucket's short name (legacy)")


@pure
def backup_target_from_env(env_content: str) -> BackupTarget | None:
    """The backup target a canonical env points at, or None for BYO backends."""
    store = backup_store_repository_from_env(env_content)
    if store is not None:
        return BackupTarget(kind=BackupTargetKind.STORE_REPOSITORY, name=store.workspace_id)
    parsed = split_backup_bucket_name_from_env(env_content)
    if parsed is not None:
        return BackupTarget(kind=BackupTargetKind.LEGACY_BUCKET, name=parsed[1])
    return None


@pure
def backup_owner_key_from_env(env_content: str) -> str | None:
    """The owner key of a canonical env's backup (store user segment or legacy bucket prefix), or None for BYO."""
    store = backup_store_repository_from_env(env_content)
    if store is not None:
        return store.user_segment
    parsed = split_backup_bucket_name_from_env(env_content)
    return parsed[0] if parsed is not None else None


@pure
def user_id_prefix_for(user_id: str) -> str:
    """Derive the legacy bucket owner prefix from a user id, exactly as the connector does."""
    return user_id.replace("-", "")[:16]


@pure
def emails_by_backup_owner_key(accounts: Mapping[str, str]) -> dict[str, str]:
    """Map each account's backup owner keys to its email (``accounts``: user_id -> email).

    Both key shapes are mapped: the backup store's hyphen-stripped user id and
    the legacy bucket prefix (its first 16 characters).
    """
    emails: dict[str, str] = {}
    for user_id, email in accounts.items():
        emails[user_id.replace("-", "")] = email
        emails[user_id_prefix_for(user_id)] = email
    return emails


def fetch_retention_seconds(connector_url: str) -> float | None:
    """Fetch the retention window from the connector's policy endpoint, or None on any failure."""
    url = f"{connector_url.rstrip('/')}{_RETENTION_POLICY_PATH}"
    try:
        response = httpx2.get(url, timeout=_RETENTION_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx2.HTTPError, ValueError) as e:
        logger.debug("Could not fetch the backup retention policy from {}: {}", url, e)
        return None
    raw_value = body.get("retention_seconds") if isinstance(body, dict) else None
    if not isinstance(raw_value, (int, float)) or raw_value <= 0:
        logger.debug("Backup retention policy from {} had an unexpected shape: {}", url, body)
        return None
    return float(raw_value)


def append_reaper_event(paths: InstallationPaths, event_type: str, payload: Mapping[str, object]) -> None:
    """Append one event to events/backup_reaper/events.jsonl (best-effort)."""
    events_path = paths.data_dir / "events" / _EVENT_SOURCE / "events.jsonl"
    envelope: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "event_id": f"evt-{uuid4().hex}",
        "source": _EVENT_SOURCE,
        **payload,
    }
    try:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a") as handle:
            handle.write(json.dumps(envelope, default=str) + "\n")
    except OSError as e:
        logger.warning("Could not append a {} event: {}", _EVENT_SOURCE, e)


def collect_destroyed_candidates(
    record_store: WorkspaceRecordStore,
    # user_id -> account email for every signed-in account
    accounts: Mapping[str, str],
) -> list[ReapCandidate]:
    """Every tombstoned record with a destroyed_at stamp, oldest first (all windows)."""
    candidates: list[ReapCandidate] = []
    for user_id, account_email in accounts.items():
        for record in record_store.list_records(user_id):
            if record.state != RECORD_STATE_DESTROYED:
                continue
            destroyed_at = parse_destroyed_at(record.destroyed_at)
            if destroyed_at is None:
                # Not stamped yet (a pre-migration row the server hasn't
                # stamped, or a pull hasn't happened) -- not reapable yet.
                continue
            candidates.append(
                ReapCandidate(
                    user_id=user_id,
                    account_email=account_email,
                    agent_id=record.agent_id,
                    host_id=record.host_id,
                    backup_bucket=record.backup_bucket,
                    display_name=record.display_name,
                    destroyed_at=destroyed_at,
                )
            )
    return sorted(candidates, key=lambda candidate: candidate.destroyed_at)


@pure
def candidate_backup_targets(candidate: ReapCandidate, env_content: str | None) -> tuple[BackupTarget, ...] | None:
    """The locations the candidate's backup may be at, most likely first; None for BYO backends.

    A known location is the only candidate: the record's explicit
    ``backup_bucket`` (a store repository path ``<user>/<workspace>`` or a
    legacy bucket's full name), else the local canonical env's repository.
    Without either the legacy bucket name is derived, workspace-keyed (the
    workspace id) first and then machine-keyed (the host id) for
    grandfathered buckets.
    """
    if candidate.backup_bucket and "/" in candidate.backup_bucket:
        return (BackupTarget(kind=BackupTargetKind.STORE_REPOSITORY, name=candidate.backup_bucket.rsplit("/", 1)[1]),)
    if candidate.backup_bucket and "--" in candidate.backup_bucket:
        return (BackupTarget(kind=BackupTargetKind.LEGACY_BUCKET, name=candidate.backup_bucket.split("--", 1)[1]),)
    if env_content is not None:
        target = backup_target_from_env(env_content)
        return (target,) if target is not None else None
    return tuple(
        BackupTarget(kind=BackupTargetKind.LEGACY_BUCKET, name=short_name)
        for short_name in dict.fromkeys((candidate.agent_id, candidate.host_id))
    )


def _is_bucket_not_found_error(error: ImbueCloudCliError) -> bool:
    return _BUCKET_NOT_FOUND_SIGNAL.lower() in f"{error.stderr} {error}".lower()


def destroy_backup_target(cli: ImbueCloudCli, account_email: str, target: BackupTarget) -> None:
    """Delete a backup target's data server-side: a store repository, or a legacy bucket (emptied first).

    A store repository that no longer exists is reported as deleted by the
    connector; a legacy bucket that is already gone raises the CLI's
    not-found error, which callers treat the same way.
    """
    match target.kind:
        case BackupTargetKind.STORE_REPOSITORY:
            cli.destroy_backup_repository(account_email, target.name)
        case BackupTargetKind.LEGACY_BUCKET:
            cli.destroy_bucket_force(account_email, target.name)
        case _ as unreachable:
            assert_never(unreachable)


def is_backup_target_present(cli: ImbueCloudCli, account_email: str, target: BackupTarget) -> bool:
    """Whether the connector still has the target: a store repository of the account, or a legacy bucket.

    Raises ``ImbueCloudCliError`` when the connector cannot answer, so an
    unreachable connector never reads as a reaped backup.
    """
    match target.kind:
        case BackupTargetKind.STORE_REPOSITORY:
            return any(info.workspace_id == target.name for info in cli.list_backup_repositories(account_email))
        case BackupTargetKind.LEGACY_BUCKET:
            try:
                cli.get_bucket_info(account_email, target.name)
            except ImbueCloudCliError as e:
                if _is_bucket_not_found_error(e):
                    return False
                raise
            return True
        case _ as unreachable:
            assert_never(unreachable)


def _destroy_first_existing_backup_target(
    imbue_cloud_cli: ImbueCloudCli, account_email: str, targets: tuple[BackupTarget, ...]
) -> BackupTarget | None:
    """Destroy the first of the candidate targets that exists; returns it, or None when none does.

    Raises ``ImbueCloudCliError`` for any failure other than a missing legacy
    bucket, so a reachable backup is never mistaken for an absent one.
    """
    for target in targets:
        try:
            destroy_backup_target(imbue_cloud_cli, account_email, target)
        except ImbueCloudCliError as e:
            if not _is_bucket_not_found_error(e):
                raise
            continue
        return target
    return None


def list_orphan_env_agent_ids(paths: InstallationPaths, record_store: WorkspaceRecordStore) -> list[AgentId]:
    """Agent ids of canonical env files referenced by no record of any account, oldest file first."""
    env_dir = backup_env_dir(paths)
    if not env_dir.is_dir():
        return []
    recorded_agent_ids = {
        record.agent_id for records in record_store.list_all_records().values() for record in records
    }
    orphan_paths = [path for path in env_dir.glob("agent-*.env") if path.stem not in recorded_agent_ids]
    orphan_paths.sort(key=lambda path: path.stat().st_mtime)
    return [AgentId(path.stem) for path in orphan_paths]


class BackupReaperManager(MutableModel):
    """Owns the periodic client-side reap pass (one background thread at a time)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    paths: InstallationPaths = Field(frozen=True, description="The Imbue Studio data-dir layout")
    record_store: WorkspaceRecordStore = Field(frozen=True, description="The workspace-record replica")
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        frozen=True, default=None, description="CLI for repository and bucket destroys; None disables backup reaping"
    )
    connector_url: str = Field(
        frozen=True, default="", description="Connector base URL for the retention policy fetch"
    )
    concurrency_group: ConcurrencyGroup | None = Field(
        frozen=True, default=None, description="Group the reap thread runs under; None disables the periodic pass"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _started_at: float = PrivateAttr(default_factory=time.monotonic)
    _last_pass_started_at: float | None = PrivateAttr(default=None)
    _cached_retention_seconds: float | None = PrivateAttr(default=None)
    _cached_retention_at: float | None = PrivateAttr(default=None)

    def maybe_start_reap_pass(self, accounts: Mapping[str, str]) -> bool:
        """Start a reap pass on a background thread when one is due; returns whether one started.

        Called from every sync-scheduler reconcile pass; the interval (and the
        single-flight lock) live here so the scheduler stays a dumb ticker.
        """
        if self.concurrency_group is None:
            return False
        now = time.monotonic()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if now - self._started_at < _REAP_STARTUP_DELAY_SECONDS:
                return False
            if self._last_pass_started_at is not None and now - self._last_pass_started_at < _REAP_INTERVAL_SECONDS:
                return False
            self._last_pass_started_at = now
            self._thread = self.concurrency_group.start_new_thread(
                target=self._run_reap_pass,
                kwargs={"accounts": dict(accounts)},
                name="backup-reaper",
                daemon=True,
                # Fire-and-forget: failures are logged (and evented) per
                # candidate; a crashed pass must not fail the app's CG exit.
                is_checked=False,
            )
        return True

    def get_retention_seconds(self) -> float:
        """The active retention window (connector-served, cached; 30-day fallback)."""
        return self._get_retention_seconds()

    def get_cached_retention_seconds(self) -> float:
        """The last-known retention window with NO network fetch (30-day fallback).

        For latency-sensitive callers that must not block on the connector (e.g.
        the destroyed-workspaces page shell, which paints before its rows load);
        :meth:`get_retention_seconds` does the authoritative cached fetch off the
        paint path.
        """
        return (
            self._cached_retention_seconds
            if self._cached_retention_seconds is not None
            else BACKUP_RETENTION_FALLBACK_SECONDS
        )

    def _get_retention_seconds(self) -> float:
        now = time.monotonic()
        if (
            self._cached_retention_seconds is not None
            and self._cached_retention_at is not None
            and now - self._cached_retention_at < _RETENTION_CACHE_TTL_SECONDS
        ):
            return self._cached_retention_seconds
        fetched = fetch_retention_seconds(self.connector_url) if self.connector_url else None
        self._cached_retention_seconds = fetched if fetched is not None else BACKUP_RETENTION_FALLBACK_SECONDS
        self._cached_retention_at = now
        return self._cached_retention_seconds

    def _run_reap_pass(self, accounts: Mapping[str, str]) -> None:
        retention_seconds = self._get_retention_seconds()
        cutoff = datetime.now(timezone.utc).timestamp() - retention_seconds
        past_window = [
            candidate
            for candidate in collect_destroyed_candidates(self.record_store, accounts)
            if candidate.destroyed_at.timestamp() < cutoff
        ]
        if not past_window:
            self._cleanup_orphan_envs(accounts)
            return
        logger.debug(
            "Backup reaper: {} candidate(s) past the {:.0f}d window; reaping up to {}",
            len(past_window),
            retention_seconds / 86400.0,
            _REAP_CAP_PER_PASS,
        )
        for candidate in past_window[:_REAP_CAP_PER_PASS]:
            self.reap_candidate(candidate, reason="retention_expired")
        self._cleanup_orphan_envs(accounts)

    def reap_candidate(self, candidate: ReapCandidate, reason: str) -> bool:
        """Reap one destroyed workspace's backup in strict order: repository, record, env.

        Any failure leaves the remaining steps untouched (the next pass
        retries); a missing repository just means another reaper got there
        first. Returns whether the candidate was fully reaped.
        """
        agent_id = AgentId(candidate.agent_id)
        if is_export_in_flight(agent_id):
            logger.debug("Backup reaper: skipping {} (export in flight)", candidate.agent_id)
            return False
        env_content = read_canonical_env(self.paths, agent_id)
        targets = candidate_backup_targets(candidate, env_content)
        if targets is not None and self.imbue_cloud_cli is not None:
            try:
                _destroy_first_existing_backup_target(self.imbue_cloud_cli, candidate.account_email, targets)
            except ImbueCloudCliError as e:
                logger.debug("Backup reaper: backup destroy for {} failed: {}", candidate.agent_id, e)
                append_reaper_event(
                    self.paths,
                    "reap_failed",
                    {"agent_id": candidate.agent_id, "host_id": candidate.host_id, "error": str(e)},
                )
                return False
        try:
            self.record_store.remove_record_or_raise(candidate.user_id, candidate.account_email, candidate.agent_id)
        except WorkspaceSyncError as e:
            logger.debug("Backup reaper: record removal for {} failed: {}", candidate.agent_id, e)
            append_reaper_event(
                self.paths,
                "reap_failed",
                {"agent_id": candidate.agent_id, "host_id": candidate.host_id, "error": str(e)},
            )
            return False
        remove_canonical_env_and_archives(self.paths, agent_id)
        logger.info(
            "Reaped destroyed machine backup: {} ({}), destroyed {}",
            candidate.display_name,
            candidate.agent_id,
            candidate.destroyed_at.isoformat(),
        )
        append_reaper_event(
            self.paths,
            "backup_reaped",
            {
                "agent_id": candidate.agent_id,
                "host_id": candidate.host_id,
                "display_name": candidate.display_name,
                "destroyed_at": candidate.destroyed_at.isoformat(),
                "reason": reason,
            },
        )
        return True

    def delete_orphan_backup_now(self, agent_id: AgentId, accounts: Mapping[str, str]) -> bool:
        """Delete an orphan env's backup on user request: repository (when its owner is signed in), then env.

        Returns False when the owning account is not signed in here or the
        repository destroy failed; True once the local env is gone.
        """
        env_content = read_canonical_env(self.paths, agent_id)
        if env_content is None:
            return True
        target = backup_target_from_env(env_content)
        owner_key = backup_owner_key_from_env(env_content)
        if target is not None and owner_key is not None:
            account_email = emails_by_backup_owner_key(accounts).get(owner_key)
            if account_email is None or self.imbue_cloud_cli is None:
                return False
            try:
                destroy_backup_target(self.imbue_cloud_cli, account_email, target)
            except ImbueCloudCliError as e:
                if not _is_bucket_not_found_error(e):
                    logger.warning("Could not destroy orphan backup for {}: {}", agent_id, e)
                    return False
        remove_canonical_env_and_archives(self.paths, agent_id)
        append_reaper_event(
            self.paths, "backup_reaped", {"agent_id": str(agent_id), "reason": "user_requested_orphan"}
        )
        return True

    def _is_target_gone(self, account_email: str, target: BackupTarget) -> bool:
        """Whether the target's data no longer exists server-side (reaped elsewhere).

        An unanswerable lookup (a connector hiccup) reads as "still there", so
        a local env is never dropped on a transient failure.
        """
        if self.imbue_cloud_cli is None:
            return False
        match target.kind:
            case BackupTargetKind.STORE_REPOSITORY:
                try:
                    repositories = self.imbue_cloud_cli.list_backup_repositories(account_email)
                except ImbueCloudCliError as e:
                    logger.debug("Backup reaper: orphan repository check for {} failed: {}", target.name, e)
                    return False
                return all(info.workspace_id != target.name for info in repositories)
            case BackupTargetKind.LEGACY_BUCKET:
                try:
                    self.imbue_cloud_cli.get_bucket_info(account_email, target.name)
                except ImbueCloudCliError as e:
                    if _is_bucket_not_found_error(e):
                        return True
                    logger.debug("Backup reaper: orphan bucket check for {} failed: {}", target.name, e)
                return False
            case _ as unreachable:
                assert_never(unreachable)

    def _cleanup_orphan_envs(self, accounts: Mapping[str, str]) -> None:
        """Delete local env files whose repository no longer exists (reaped elsewhere).

        The server reaper owns *aging* orphan repositories (it holds the
        first-seen stamps); this pass only cleans up the local leftovers once
        the repository is gone, and only for accounts signed in here
        (lookups are owner-scoped).
        """
        if self.imbue_cloud_cli is None:
            return
        email_by_owner_key = emails_by_backup_owner_key(accounts)
        for agent_id in list_orphan_env_agent_ids(self.paths, self.record_store)[:_REAP_CAP_PER_PASS]:
            env_content = read_canonical_env(self.paths, agent_id)
            if env_content is None:
                continue
            target = backup_target_from_env(env_content)
            owner_key = backup_owner_key_from_env(env_content)
            if target is None or owner_key is None:
                # BYO backend: the repository is not ours to check or delete;
                # leave the env for the user.
                continue
            account_email = email_by_owner_key.get(owner_key)
            if account_email is None:
                continue
            if self._is_target_gone(account_email, target):
                remove_canonical_env_and_archives(self.paths, agent_id)
                logger.info("Removed orphan restic.env for {}: its backup repository is gone", agent_id)
                append_reaper_event(self.paths, "orphan_env_cleaned", {"agent_id": str(agent_id)})


def evict_oldest_reapable_backup(
    *,
    record_store: WorkspaceRecordStore,
    paths: InstallationPaths,
    imbue_cloud_cli: ImbueCloudCli,
    user_id: str,
    account_email: str,
) -> bool:
    """Evict the oldest destroyed-workspace backup for this account to relieve quota pressure.

    Candidates are the same set the reapers act on -- tombstoned records
    (oldest destroyed first, past-window before within-window by ordering) --
    scoped to this account. Returns True when a repository was actually
    destroyed (quota was freed); False when there is nothing left to evict.
    Raises ``ImbueCloudCliError`` on a destroy failure so the provisioning
    attempt aborts rather than looping.
    """
    manager = BackupReaperManager(
        paths=paths, record_store=record_store, imbue_cloud_cli=imbue_cloud_cli, connector_url=""
    )
    for candidate in collect_destroyed_candidates(record_store, {user_id: account_email}):
        agent_id = AgentId(candidate.agent_id)
        if is_export_in_flight(agent_id):
            # The backup is mid-download; destroying its repository would
            # corrupt the export. Fall through to the next-oldest candidate.
            logger.debug("Quota eviction: skipping {} (export in flight)", candidate.agent_id)
            continue
        env_content = read_canonical_env(paths, agent_id)
        targets = candidate_backup_targets(candidate, env_content)
        if targets is None:
            # BYO backend: destroying it would not free imbue_cloud quota.
            continue
        if _destroy_first_existing_backup_target(imbue_cloud_cli, account_email, targets) is None:
            # No backup at any candidate location: clean up the leftovers and
            # keep looking for a candidate that actually frees quota.
            manager.reap_candidate(candidate, reason="quota_eviction_leftover")
            continue
        # Repository destroyed: finish the atomic early-delete (record + env).
        try:
            record_store.remove_record_or_raise(user_id, account_email, candidate.agent_id)
        except WorkspaceSyncError as e:
            logger.warning("Evicted {}'s backup but could not remove its record: {}", candidate.agent_id, e)
        remove_canonical_env_and_archives(paths, agent_id)
        logger.info(
            "Evicted destroyed machine backup to free quota: {} ({}), destroyed {}",
            candidate.display_name,
            candidate.agent_id,
            candidate.destroyed_at.isoformat(),
        )
        append_reaper_event(
            paths,
            "backup_evicted",
            {
                "agent_id": candidate.agent_id,
                "host_id": candidate.host_id,
                "display_name": candidate.display_name,
                "destroyed_at": candidate.destroyed_at.isoformat(),
                "reason": "quota_pressure",
            },
        )
        return True
    return False


def make_quota_evictor(
    *,
    record_store: WorkspaceRecordStore,
    paths: InstallationPaths,
    imbue_cloud_cli: ImbueCloudCli,
    user_id: str,
    account_email: str,
) -> Callable[[], bool]:
    """Bind an eviction callback for one account, for backup provisioning's quota retry loop."""
    return lambda: evict_oldest_reapable_backup(
        record_store=record_store,
        paths=paths,
        imbue_cloud_cli=imbue_cloud_cli,
        user_id=user_id,
        account_email=account_email,
    )
