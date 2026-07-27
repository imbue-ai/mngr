"""Client-side reaper for destroyed workspaces' backups, plus quota-pressure eviction.

Destroyed workspaces keep their backup (R2 bucket + workspace record + local
``restic.env``) for a retention window (served by the connector, 30 days) and
are then reaped: bucket first (emptied client-side via the CLI's force
destroy), then the record, then the local env. The reaper runs on its own
cadence inside the sync scheduler's reconcile loop, on a background thread
under the app's concurrency group, guarded by a lock so only one pass is ever
in flight. The connector runs a server-side backstop hourly; every step here
is idempotent so the two never conflict.

Quota-pressure eviction reuses the same candidate set: when backup
provisioning hits a bucket-count or storage quota, the oldest destroyed
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
from typing import Final
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_env_store import backup_env_dir
from imbue.minds.desktop_client.backup_env_store import canonical_env_path
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
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

# An imbue_cloud backup repository lives on R2; anything else is a BYO
# backend whose storage is not ours to delete.
_R2_ENDPOINT_MARKER: Final[str] = ".r2.cloudflarestorage.com"

# Events land under events/<source>/events.jsonl in the minds data dir.
_EVENT_SOURCE: Final[str] = "backup_reaper"

# The CLI writes the raising exception's class name into its stderr JSON;
# a missing bucket during a reap just means someone else got there first.
_BUCKET_NOT_FOUND_SIGNAL: Final[str] = "R2BucketNotFoundError"


class ReapCandidate(FrozenModel):
    """One destroyed workspace whose backup is eligible for reaping/eviction."""

    user_id: str = Field(description="Owning account's user id")
    account_email: str = Field(description="Owning account's email (CLI --account)")
    agent_id: str = Field(description="Workspace agent id (keys the local env file)")
    host_id: str = Field(description="Workspace host id (names the backup bucket)")
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


@pure
def _split_backup_bucket_name_from_env(env_content: str) -> tuple[str, str] | None:
    """``(owner_prefix, short_name)`` of the backup bucket from a canonical env, or None for BYO backends.

    imbue_cloud repositories look like
    ``s3:https://<acct>.r2.cloudflarestorage.com/<user-prefix>--<host-id>``;
    the short name is everything after the owner-prefix separator.
    """
    repository = parse_restic_env(env_content).get("RESTIC_REPOSITORY", "")
    if _R2_ENDPOINT_MARKER not in repository:
        return None
    bucket_name = repository.rstrip("/").rsplit("/", 1)[-1]
    if "--" not in bucket_name:
        return None
    owner_prefix, _, short_name = bucket_name.partition("--")
    return owner_prefix, short_name


@pure
def bucket_short_name_from_env(env_content: str) -> str | None:
    """The backup bucket's short name (its host id) from a canonical env, or None for BYO backends."""
    parsed = _split_backup_bucket_name_from_env(env_content)
    return parsed[1] if parsed is not None else None


@pure
def bucket_owner_prefix_from_env(env_content: str) -> str | None:
    """The backup bucket's owner prefix from a canonical env, or None for BYO backends."""
    parsed = _split_backup_bucket_name_from_env(env_content)
    return parsed[0] if parsed is not None else None


@pure
def user_id_prefix_for(user_id: str) -> str:
    """Derive the bucket owner prefix from a user id, exactly as the connector does."""
    return user_id.replace("-", "")[:16]


@pure
def emails_by_bucket_owner_prefix(accounts: Mapping[str, str]) -> dict[str, str]:
    """Map each account's bucket owner prefix to its email (``accounts``: user_id -> email)."""
    return {user_id_prefix_for(user_id): email for user_id, email in accounts.items()}


def fetch_retention_seconds(connector_url: str) -> float | None:
    """Fetch the retention window from the connector's policy endpoint, or None on any failure."""
    url = f"{connector_url.rstrip('/')}{_RETENTION_POLICY_PATH}"
    try:
        response = httpx.get(url, timeout=_RETENTION_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("Could not fetch the backup retention policy from {}: {}", url, e)
        return None
    raw_value = body.get("retention_seconds") if isinstance(body, dict) else None
    if not isinstance(raw_value, (int, float)) or raw_value <= 0:
        logger.debug("Backup retention policy from {} had an unexpected shape: {}", url, body)
        return None
    return float(raw_value)


def append_reaper_event(paths: WorkspacePaths, event_type: str, payload: Mapping[str, object]) -> None:
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
                    display_name=record.display_name,
                    destroyed_at=destroyed_at,
                )
            )
    return sorted(candidates, key=lambda candidate: candidate.destroyed_at)


def _is_bucket_not_found_error(error: ImbueCloudCliError) -> bool:
    return _BUCKET_NOT_FOUND_SIGNAL.lower() in f"{error.stderr} {error}".lower()


def list_orphan_env_agent_ids(paths: WorkspacePaths, record_store: WorkspaceRecordStore) -> list[AgentId]:
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

    paths: WorkspacePaths = Field(frozen=True, description="The minds data-dir layout")
    record_store: WorkspaceRecordStore = Field(frozen=True, description="The workspace-record replica")
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        frozen=True, default=None, description="CLI for bucket destroys; None disables bucket reaping"
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
        """Reap one destroyed workspace's backup in strict order: bucket, record, env.

        Any failure leaves the remaining steps untouched (the next pass
        retries); a missing bucket just means another reaper got there first.
        Returns whether the candidate was fully reaped.
        """
        agent_id = AgentId(candidate.agent_id)
        if is_export_in_flight(agent_id):
            logger.debug("Backup reaper: skipping {} (export in flight)", candidate.agent_id)
            return False
        env_content = read_canonical_env(self.paths, agent_id)
        bucket_short_name = bucket_short_name_from_env(env_content) if env_content is not None else None
        if bucket_short_name is None and env_content is None:
            # No local env: the bucket (if any) is named by the host id.
            bucket_short_name = candidate.host_id
        if bucket_short_name is not None and self.imbue_cloud_cli is not None:
            try:
                self.imbue_cloud_cli.destroy_bucket_force(candidate.account_email, bucket_short_name)
            except ImbueCloudCliError as e:
                if not _is_bucket_not_found_error(e):
                    logger.debug("Backup reaper: bucket destroy for {} failed: {}", candidate.agent_id, e)
                    append_reaper_event(
                        self.paths,
                        "reap_failed",
                        {"agent_id": candidate.agent_id, "host_id": candidate.host_id, "error": str(e)},
                    )
                    return False
        try:
            self.record_store.remove_record_or_raise(candidate.user_id, candidate.account_email, candidate.host_id)
        except WorkspaceSyncError as e:
            logger.debug("Backup reaper: record removal for {} failed: {}", candidate.agent_id, e)
            append_reaper_event(
                self.paths,
                "reap_failed",
                {"agent_id": candidate.agent_id, "host_id": candidate.host_id, "error": str(e)},
            )
            return False
        canonical_env_path(self.paths, agent_id).unlink(missing_ok=True)
        logger.info(
            "Reaped destroyed workspace backup: {} ({}), destroyed {}",
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
        """Delete an orphan env's backup on user request: bucket (when its owner is signed in), then env.

        Returns False when the owning account is not signed in here or the
        bucket destroy failed; True once the local env is gone.
        """
        env_content = read_canonical_env(self.paths, agent_id)
        if env_content is None:
            return True
        bucket_short_name = bucket_short_name_from_env(env_content)
        owner_prefix = bucket_owner_prefix_from_env(env_content)
        if bucket_short_name is not None and owner_prefix is not None:
            account_email = emails_by_bucket_owner_prefix(accounts).get(owner_prefix)
            if account_email is None or self.imbue_cloud_cli is None:
                return False
            try:
                self.imbue_cloud_cli.destroy_bucket_force(account_email, bucket_short_name)
            except ImbueCloudCliError as e:
                if not _is_bucket_not_found_error(e):
                    logger.warning("Could not destroy orphan backup bucket for {}: {}", agent_id, e)
                    return False
        canonical_env_path(self.paths, agent_id).unlink(missing_ok=True)
        append_reaper_event(
            self.paths, "backup_reaped", {"agent_id": str(agent_id), "reason": "user_requested_orphan"}
        )
        return True

    def _cleanup_orphan_envs(self, accounts: Mapping[str, str]) -> None:
        """Delete local env files whose bucket no longer exists (reaped elsewhere).

        The server reaper owns *aging* orphan buckets (it holds the first-seen
        stamps); this pass only cleans up the local leftovers once the bucket
        is gone, and only for accounts signed in here (bucket lookups are
        owner-scoped).
        """
        if self.imbue_cloud_cli is None:
            return
        email_by_prefix = emails_by_bucket_owner_prefix(accounts)
        for agent_id in list_orphan_env_agent_ids(self.paths, self.record_store)[:_REAP_CAP_PER_PASS]:
            env_content = read_canonical_env(self.paths, agent_id)
            if env_content is None:
                continue
            bucket_short_name = bucket_short_name_from_env(env_content)
            owner_prefix = bucket_owner_prefix_from_env(env_content)
            if bucket_short_name is None or owner_prefix is None:
                # BYO backend: the repository is not ours to check or delete;
                # leave the env for the user.
                continue
            account_email = email_by_prefix.get(owner_prefix)
            if account_email is None:
                continue
            try:
                self.imbue_cloud_cli.get_bucket_info(account_email, bucket_short_name)
            except ImbueCloudCliError as e:
                if not _is_bucket_not_found_error(e):
                    logger.debug("Backup reaper: orphan bucket check for {} failed: {}", agent_id, e)
                    continue
                canonical_env_path(self.paths, agent_id).unlink(missing_ok=True)
                logger.info("Removed orphan restic.env for {}: its bucket is gone", agent_id)
                append_reaper_event(self.paths, "orphan_env_cleaned", {"agent_id": str(agent_id)})


def evict_oldest_reapable_backup(
    *,
    record_store: WorkspaceRecordStore,
    paths: WorkspacePaths,
    imbue_cloud_cli: ImbueCloudCli,
    user_id: str,
    account_email: str,
) -> bool:
    """Evict the oldest destroyed-workspace backup for this account to relieve quota pressure.

    Candidates are the same set the reapers act on -- tombstoned records
    (oldest destroyed first, past-window before within-window by ordering) --
    scoped to this account. Returns True when a bucket was actually destroyed
    (quota was freed); False when there is nothing left to evict. Raises
    ``ImbueCloudCliError`` on a destroy failure so the provisioning attempt
    aborts rather than looping.
    """
    manager = BackupReaperManager(
        paths=paths, record_store=record_store, imbue_cloud_cli=imbue_cloud_cli, connector_url=""
    )
    for candidate in collect_destroyed_candidates(record_store, {user_id: account_email}):
        agent_id = AgentId(candidate.agent_id)
        if is_export_in_flight(agent_id):
            # The backup is mid-download; destroying its bucket would corrupt
            # the export. Fall through to the next-oldest candidate.
            logger.debug("Quota eviction: skipping {} (export in flight)", candidate.agent_id)
            continue
        env_content = read_canonical_env(paths, agent_id)
        bucket_short_name = bucket_short_name_from_env(env_content) if env_content is not None else candidate.host_id
        if bucket_short_name is None:
            # BYO backend: destroying it would not free imbue_cloud quota.
            continue
        try:
            imbue_cloud_cli.destroy_bucket_force(account_email, bucket_short_name)
        except ImbueCloudCliError as e:
            if not _is_bucket_not_found_error(e):
                raise
            # Bucket already gone: clean up the leftovers and keep looking
            # for a candidate that actually frees quota.
            manager.reap_candidate(candidate, reason="quota_eviction_leftover")
            continue
        # Bucket destroyed: finish the atomic early-delete (record + env).
        try:
            record_store.remove_record_or_raise(user_id, account_email, candidate.host_id)
        except WorkspaceSyncError as e:
            logger.warning("Evicted {}'s bucket but could not remove its record: {}", candidate.agent_id, e)
        canonical_env_path(paths, agent_id).unlink(missing_ok=True)
        logger.info(
            "Evicted destroyed workspace backup to free quota: {} ({}), destroyed {}",
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
    paths: WorkspacePaths,
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
