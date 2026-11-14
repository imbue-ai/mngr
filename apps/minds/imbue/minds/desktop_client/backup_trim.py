"""Client-side backup trim flow: free storage for an account over its backup quota.

The backup store refuses new backup data once an account is past its hard
limit, which sits a headroom above the soft quota so restic can still reclaim
space (prune repacks, which writes new packs before it deletes the old ones).
The flow is: forget the oldest half of every reachable repository's snapshots
(never the latest), prune with a repack cap of half that headroom, re-read the
account's usage, and repeat until the account is under its soft quota or
nothing is left to trim. Repositories whose canonical ``restic.env`` is not on
this machine are reported by name; workspaces still on legacy bucket storage
are counted and left alone, since the store's usage does not include them.

The flow runs on a detached daemon thread (like backup provisioning); the
Accounts page polls its progress from :class:`BackupTrimManager`.
"""

import threading
from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import backup_env_dir
from imbue.minds.desktop_client.backup_env_store import is_legacy_bucket_repository
from imbue.minds.desktop_client.backup_env_store import parse_backup_store_repository
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import split_backup_bucket_name_from_env
from imbue.minds.desktop_client.imbue_cloud_cli import BackupStorageUsage
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.notification_feed import SystemEventCard
from imbue.minds.desktop_client.restic_cli import ResticSnapshot
from imbue.minds.desktop_client.restic_cli import forget_snapshots
from imbue.minds.desktop_client.restic_cli import list_snapshots
from imbue.minds.desktop_client.restic_cli import prune_repository
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.errors import BackupProvisioningError

# Each round forgets the oldest half of every reachable repo's snapshots, so
# a handful of rounds reduces any repo to its latest snapshot; the cap only
# guards against a pathological loop.
_MAX_TRIM_ROUNDS: Final = 8

BACKUP_CLEANUP_UNFINISHED_SENTENCE: Final[str] = (
    "Backup cleanup didn't finish. Try again. If this persists, report a bug."
)


class BackupTrimState(UpperCaseStrEnum):
    """Lifecycle state of one account's backup-trim run."""

    RUNNING = auto()
    SUCCEEDED = auto()
    FAILED = auto()


class BackupTrimStatus(FrozenModel):
    """Progress of one account's backup-trim run, rendered on the Accounts page."""

    state: BackupTrimState = Field(description="Whether the run is still going and how it ended")
    detail: str = Field(description="Human-readable progress / outcome line")
    error_detail: str = Field(
        description="What went wrong, for See details, when the run could not finish; '' otherwise"
    )

    @property
    def is_running(self) -> bool:
        return self.state == BackupTrimState.RUNNING


class TrimmableRepo(FrozenModel):
    """One locally-reachable backup-store repository, keyed by its workspace id."""

    workspace_id: str = Field(description="The workspace whose repository this is")
    repository: str = Field(description="restic repository URL (rest:<store>/<user>/<workspace>/)")
    password: str = Field(description="The workspace's RESTIC_PASSWORD (may be empty)")
    backend_env: dict[str, str] = Field(description="Backend credential env vars (RESTIC_REST_* etc.)")


class TrimmableRepoCollection(FrozenModel):
    """What this machine's canonical envs hold for one account: trimmable store repositories and legacy leftovers."""

    repo_by_workspace_id: dict[str, TrimmableRepo] = Field(description="Backup-store repositories, by workspace id")
    legacy_repository_count: int = Field(
        description="This account's envs still on a legacy bucket (not yet migrated, so not counted by the store)"
    )


@pure
def user_segment_for(user_id: str) -> str:
    """The backup store's user path segment: the SuperTokens user id with hyphens stripped."""
    return user_id.replace("-", "")


@pure
def legacy_owner_prefix_for(user_segment: str) -> str:
    """The legacy bucket owner prefix of the same account: the segment's first 16 characters."""
    return user_segment[:16]


@pure
def max_repack_bytes_for(usage: BackupStorageUsage) -> int | None:
    """Half the hard-limit headroom, the repack cap that keeps one prune's transient growth inside it."""
    headroom = max(usage.hard_limit_bytes - usage.quota_bytes, 0)
    return headroom // 2 if headroom > 0 else None


@pure
def select_snapshot_ids_to_forget(snapshots: Sequence[ResticSnapshot]) -> list[str]:
    """Pick the oldest half of the snapshots to forget (never the latest).

    ``len // 2`` is always at most ``len - 1``, so at least the newest
    snapshot survives every round; a single-snapshot repo is never trimmed.
    """
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.time)
    forget_count = len(ordered) // 2
    return [snapshot.snapshot_id for snapshot in ordered[:forget_count]]


def collect_trimmable_repos(paths: InstallationPaths, *, user_segment: str) -> TrimmableRepoCollection:
    """Parse every canonical restic env on this machine into the account's trimmable repositories.

    Only backup-store repositories owned by ``user_segment`` are trimmable;
    the account's legacy bucket repositories (workspaces the store migration
    has not reached yet) are counted, and everything else is another
    account's or a bring-your-own backend that is not ours to trim. An
    unreadable env file is skipped with a warning rather than failing the
    whole trim.
    """
    env_dir = backup_env_dir(paths)
    if not env_dir.is_dir():
        return TrimmableRepoCollection(repo_by_workspace_id={}, legacy_repository_count=0)
    repos: dict[str, TrimmableRepo] = {}
    legacy_count = 0
    for env_path in sorted(env_dir.glob("*.env")):
        try:
            content = env_path.read_text()
        except OSError as exc:
            logger.warning("Skipping unreadable canonical restic env {}: {}", env_path, exc)
            continue
        parsed = parse_restic_env(content)
        repository = parsed.get("RESTIC_REPOSITORY", "")
        if is_legacy_bucket_repository(repository):
            legacy_bucket = split_backup_bucket_name_from_env(content)
            if legacy_bucket is not None and legacy_bucket[0] == legacy_owner_prefix_for(user_segment):
                legacy_count += 1
            continue
        store = parse_backup_store_repository(repository)
        if store is None or store.user_segment != user_segment:
            continue
        backend_env = {
            key: value for key, value in parsed.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")
        }
        repos[store.workspace_id] = TrimmableRepo(
            workspace_id=store.workspace_id,
            repository=repository,
            password=parsed.get("RESTIC_PASSWORD", ""),
            backend_env=backend_env,
        )
    return TrimmableRepoCollection(repo_by_workspace_id=repos, legacy_repository_count=legacy_count)


def run_backup_trim(
    *,
    account_email: str,
    user_id: str,
    cli: ImbueCloudCli,
    paths: InstallationPaths,
    report_progress: Callable[[str], None],
    # Injectable (fakes in tests); production callers pass the restic_cli
    # functions of the same names.
    list_snapshots_fn: Callable[..., tuple[ResticSnapshot, ...]],
    forget_snapshots_fn: Callable[..., None],
    prune_fn: Callable[..., None],
) -> tuple[bool, str]:
    """Trim old snapshots in rounds until the account is under its backup quota.

    Returns ``(is_under_quota, human-readable outcome detail)``. Raises
    ``ImbueCloudCliError`` / ``BackupProvisioningError`` on a failed
    connector call or restic invocation; the caller records those as the
    run's failure outcome.
    """
    usage = cli.get_backup_usage(account_email)
    if usage.used_bytes <= usage.quota_bytes:
        return True, "Backup storage is already under its limit."
    max_repack_bytes = max_repack_bytes_for(usage)

    collection = collect_trimmable_repos(paths, user_segment=user_segment_for(user_id))
    repos = collection.repo_by_workspace_id
    server_workspace_ids = sorted({info.workspace_id for info in cli.list_backup_repositories(account_email)})
    untrimmable_ids = [workspace_id for workspace_id in server_workspace_ids if workspace_id not in repos]
    # A local env whose repository the store no longer lists (reaped
    # elsewhere, or re-keyed by another device) cannot be opened, so only the
    # repositories known on both sides are trimmed.
    trimmable_ids = [workspace_id for workspace_id in server_workspace_ids if workspace_id in repos]

    for round_idx in range(_MAX_TRIM_ROUNDS):
        # Forget the oldest half of each reachable repo's snapshots, then
        # reclaim the space with a repack cap that fits the headroom.
        is_any_forgotten = False
        for workspace_id in trimmable_ids:
            repo = repos[workspace_id]
            snapshots = list_snapshots_fn(
                repository=repo.repository, backend_env=repo.backend_env, password=repo.password
            )
            snapshot_ids_to_forget = select_snapshot_ids_to_forget(snapshots)
            if not snapshot_ids_to_forget:
                continue
            report_progress(
                f"Round {round_idx + 1}: removing {len(snapshot_ids_to_forget)} of {len(snapshots)} "
                f"old backups from {workspace_id}..."
            )
            forget_snapshots_fn(
                repository=repo.repository,
                backend_env=repo.backend_env,
                password=repo.password,
                snapshot_ids=snapshot_ids_to_forget,
            )
            prune_fn(
                repository=repo.repository,
                backend_env=repo.backend_env,
                password=repo.password,
                max_repack_bytes=max_repack_bytes,
            )
            is_any_forgotten = True

        report_progress(f"Round {round_idx + 1}: re-measuring storage usage...")
        usage = cli.get_backup_usage(account_email)
        if usage.used_bytes <= usage.quota_bytes:
            return True, "Backup storage is back under its limit; new backups will resume."
        if not is_any_forgotten:
            break

    legacy_sentence = (
        f" {collection.legacy_repository_count} backup(s) are still on legacy storage, which the backup store does"
        " not count, and were not trimmed."
        if collection.legacy_repository_count
        else ""
    )
    if untrimmable_ids:
        return False, (
            "You're still over your backup storage limit. These workspaces' backups can only be deleted from "
            f"the computer that created them: {', '.join(untrimmable_ids)}. "
            "Otherwise, remove workspaces you no longer need." + legacy_sentence
        )
    return False, (
        "You're still over your backup storage limit, and each workspace keeps at least its latest backup. "
        "Remove workspaces you no longer need." + legacy_sentence
    )


class BackupTrimManager(MutableModel):
    """Runs at most one backup-trim per account at a time and tracks its progress."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status_by_user_id: dict[str, BackupTrimStatus] = Field(
        default_factory=dict, description="Latest trim status per account user id"
    )
    status_lock: threading.Lock = Field(
        default_factory=threading.Lock,
        description="Guards status_by_user_id (written by worker threads, read by request threads)",
    )

    def get_status(self, user_id: str) -> BackupTrimStatus | None:
        with self.status_lock:
            return self.status_by_user_id.get(user_id)

    def is_any_running(self) -> bool:
        with self.status_lock:
            return any(status.is_running for status in self.status_by_user_id.values())

    def start_trim(
        self,
        *,
        user_id: str,
        account_email: str,
        cli: ImbueCloudCli,
        paths: InstallationPaths,
        notification_feed: NotificationFeed | None,
    ) -> bool:
        """Start a trim run on a detached thread; returns False when one is already running."""
        with self.status_lock:
            existing = self.status_by_user_id.get(user_id)
            if existing is not None and existing.is_running:
                return False
            self.status_by_user_id[user_id] = BackupTrimStatus(
                state=BackupTrimState.RUNNING, detail="Starting backup cleanup...", error_detail=""
            )
        thread = threading.Thread(
            target=self._run,
            kwargs={
                "user_id": user_id,
                "account_email": account_email,
                "cli": cli,
                "paths": paths,
                "notification_feed": notification_feed,
            },
            name=f"backup-trim-{user_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _set_status(self, user_id: str, state: BackupTrimState, detail: str, error_detail: str) -> None:
        with self.status_lock:
            self.status_by_user_id[user_id] = BackupTrimStatus(state=state, detail=detail, error_detail=error_detail)

    def _run(
        self,
        *,
        user_id: str,
        account_email: str,
        cli: ImbueCloudCli,
        paths: InstallationPaths,
        notification_feed: NotificationFeed | None,
    ) -> None:
        try:
            is_under_quota, detail = run_backup_trim(
                account_email=account_email,
                user_id=user_id,
                cli=cli,
                paths=paths,
                report_progress=lambda progress_detail: self._set_status(
                    user_id, BackupTrimState.RUNNING, progress_detail, ""
                ),
                list_snapshots_fn=list_snapshots,
                forget_snapshots_fn=forget_snapshots,
                prune_fn=prune_repository,
            )
            self._set_status(
                user_id, BackupTrimState.SUCCEEDED if is_under_quota else BackupTrimState.FAILED, detail, ""
            )
        except (BackupProvisioningError, ImbueCloudCliError) as exc:
            logger.opt(exception=exc).warning("Backup trim for {} failed", account_email)
            self._set_status(user_id, BackupTrimState.FAILED, BACKUP_CLEANUP_UNFINISHED_SENTENCE, str(exc))
        finally:
            # A crash from an unexpected exception type must not leave the
            # status stuck on "running" (the page would show a live cleanup
            # forever); flip it to failed before the thread dies.
            with self.status_lock:
                current = self.status_by_user_id.get(user_id)
                if current is not None and current.is_running:
                    self.status_by_user_id[user_id] = BackupTrimStatus(
                        state=BackupTrimState.FAILED,
                        detail=BACKUP_CLEANUP_UNFINISHED_SENTENCE,
                        error_detail="The cleanup stopped unexpectedly; the app's log has the error.",
                    )
            # In the finally, so a crash still reaches the feed.
            self._append_outcome_to_feed(user_id, account_email, notification_feed)

    def _append_outcome_to_feed(
        self, user_id: str, account_email: str, notification_feed: NotificationFeed | None
    ) -> None:
        outcome = self.get_status(user_id)
        if notification_feed is not None and outcome is not None:
            # Account-level: the cleanup concerns the account's quota, not one
            # workspace, so the entry names the account and its click lands on
            # the accounts page the cleanup was started from.
            notification_feed.append_system_event(
                SystemEventCard(
                    title="Backup cleanup finished"
                    if outcome.state == BackupTrimState.SUCCEEDED
                    else "Backup cleanup failed",
                    body=outcome.detail,
                    workspace_agent_id="",
                    account_id=user_id,
                    workspace_name=account_email,
                    workspace_accent=DEFAULT_WORKSPACE_COLOR,
                )
            )
