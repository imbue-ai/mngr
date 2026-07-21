"""Client-side backup trim flow: free storage for an over-quota account.

An account over its storage quota has read-only bucket keys, and restic's
space reclaim (``forget`` + ``prune``) needs write access, so the flow runs
under a connector cleanup grant: request the grant (temporarily restoring
readwrite), forget the oldest half of every reachable repository's snapshots
(never the latest), prune, recheck (settling the grant and re-measuring live
usage), and repeat until the account is under quota or nothing is left to
trim. Repositories whose canonical ``restic.env`` is not on this machine are
skipped and reported by name.

The flow runs on a detached daemon thread (like backup provisioning); the
Accounts page polls its progress from :class:`BackupTrimManager`.
"""

import threading
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_env_store import backup_env_dir
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.desktop_client.restic_cli import ResticSnapshot
from imbue.minds.desktop_client.restic_cli import forget_snapshots
from imbue.minds.desktop_client.restic_cli import list_snapshots
from imbue.minds.errors import BackupProvisioningError

# Each round forgets the oldest half of every reachable repo's snapshots, so
# a handful of rounds reduces any repo to its latest snapshot; the cap only
# guards against a pathological loop.
_MAX_TRIM_ROUNDS: Final = 8

_RUNNING: Final = "running"
_SUCCEEDED: Final = "succeeded"
_FAILED: Final = "failed"


class BackupTrimStatus(FrozenModel):
    """Progress of one account's backup-trim run, rendered on the Accounts page."""

    state: str = Field(description="'running', 'succeeded', or 'failed'")
    detail: str = Field(description="Human-readable progress / outcome line")

    @property
    def is_running(self) -> bool:
        return self.state == _RUNNING


class TrimmableRepo(FrozenModel):
    """One locally-reachable restic repository, keyed by its full bucket name."""

    bucket_name: str = Field(description="Full R2 bucket name the repository lives in")
    repository: str = Field(description="restic repository URL (s3:<endpoint>/<bucket>)")
    password: str = Field(description="The workspace's RESTIC_PASSWORD (may be empty)")
    backend_env: dict[str, str] = Field(description="Backend credential env vars (AWS_* etc.)")


@pure
def bucket_name_from_repository(repository: str) -> str | None:
    """Extract the bucket name from a restic S3 repository URL (``s3:<endpoint>/<bucket>[/...]``)."""
    if not repository.startswith("s3:"):
        return None
    remainder = repository[len("s3:") :]
    scheme_split = remainder.split("://", 1)
    path_part = scheme_split[1] if len(scheme_split) == 2 else scheme_split[0]
    segments = [segment for segment in path_part.split("/") if segment]
    if len(segments) < 2:
        return None
    return segments[1]


@pure
def select_snapshot_ids_to_forget(snapshots: Sequence[ResticSnapshot]) -> list[str]:
    """Pick the oldest half of the snapshots to forget (never the latest).

    ``len // 2`` is always at most ``len - 1``, so at least the newest
    snapshot survives every round; a single-snapshot repo is never trimmed.
    """
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.time)
    forget_count = len(ordered) // 2
    return [snapshot.snapshot_id for snapshot in ordered[:forget_count]]


def collect_trimmable_repos(paths: WorkspacePaths) -> dict[str, TrimmableRepo]:
    """Parse every canonical restic env on this machine, keyed by full bucket name.

    Only S3-style repositories (the imbue_cloud backup shape) are included;
    an unreadable env file is skipped with a warning rather than failing the
    whole trim.
    """
    env_dir = backup_env_dir(paths)
    if not env_dir.is_dir():
        return {}
    repos: dict[str, TrimmableRepo] = {}
    for env_path in sorted(env_dir.glob("*.env")):
        try:
            content = env_path.read_text()
        except OSError as exc:
            logger.warning("Skipping unreadable canonical restic env {}: {}", env_path, exc)
            continue
        parsed = parse_restic_env(content)
        repository = parsed.get("RESTIC_REPOSITORY", "")
        bucket_name = bucket_name_from_repository(repository)
        if bucket_name is None:
            continue
        backend_env = {
            key: value for key, value in parsed.items() if key not in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")
        }
        repos[bucket_name] = TrimmableRepo(
            bucket_name=bucket_name,
            repository=repository,
            password=parsed.get("RESTIC_PASSWORD", ""),
            backend_env=backend_env,
        )
    return repos


def run_backup_trim(
    *,
    account_email: str,
    cli: ImbueCloudCli,
    paths: WorkspacePaths,
    report_progress: Callable[[str], None],
    # Injected for tests; production callers use the restic_cli functions.
    list_snapshots_fn: Callable[..., tuple[ResticSnapshot, ...]] = list_snapshots,
    forget_snapshots_fn: Callable[..., None] = forget_snapshots,
    # (is_under_quota, human-readable outcome detail)
) -> tuple[bool, str]:
    """Trim old snapshots in rounds until the account is under its storage quota.

    Raises ``ImbueCloudCliError`` / ``BackupProvisioningError`` on a failed
    connector call or restic invocation; the caller records those as the
    run's failure outcome.
    """
    initial = cli.recheck_storage(account_email)
    if not bool(initial.get("is_over_quota")):
        return True, "Backup storage is already under its limit; backups are writable."

    repos = collect_trimmable_repos(paths)
    untrimmable_names: list[str] = []
    for round_idx in range(_MAX_TRIM_ROUNDS):
        # The grant restores the downgraded keys (idempotent while active)
        # and tells us which buckets the account has keys for.
        grant = cli.create_storage_cleanup_grant(account_email)
        raw_keys = grant.get("keys") or []
        bucket_names = sorted(
            {
                str(entry.get("bucket_name"))
                for entry in raw_keys
                if isinstance(entry, dict) and entry.get("bucket_name")
            }
        )
        untrimmable_names = [name for name in bucket_names if name not in repos]

        # Forget the oldest half of each reachable repo's snapshots and prune.
        is_any_forgotten = False
        for bucket_name in bucket_names:
            repo = repos.get(bucket_name)
            if repo is None:
                continue
            snapshots = list_snapshots_fn(
                repository=repo.repository, backend_env=repo.backend_env, password=repo.password
            )
            snapshot_ids_to_forget = select_snapshot_ids_to_forget(snapshots)
            if not snapshot_ids_to_forget:
                continue
            report_progress(
                f"Round {round_idx + 1}: removing {len(snapshot_ids_to_forget)} of {len(snapshots)} "
                f"old backups from {bucket_name}..."
            )
            forget_snapshots_fn(
                repository=repo.repository,
                backend_env=repo.backend_env,
                password=repo.password,
                snapshot_ids=snapshot_ids_to_forget,
                is_pruning=True,
            )
            is_any_forgotten = True

        # Re-measure and settle: restores keys when under, re-downgrades when
        # still over (which also settles the grant against its baseline).
        report_progress(f"Round {round_idx + 1}: re-measuring storage usage...")
        rechecked = cli.recheck_storage(account_email)
        if not bool(rechecked.get("is_over_quota")):
            return True, "Backup storage is back under its limit; backups are writable again."
        if not is_any_forgotten:
            break

    untrimmable_suffix = (
        f" Backups not reachable from this machine (delete their workspaces or clean them up elsewhere): "
        f"{', '.join(untrimmable_names)}."
        if untrimmable_names
        else ""
    )
    return False, (
        "Still over the storage limit after trimming old backups "
        "(each workspace keeps at least its latest backup)." + untrimmable_suffix
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
        paths: WorkspacePaths,
        notification_dispatcher: NotificationDispatcher | None,
    ) -> bool:
        """Start a trim run on a detached thread; returns False when one is already running."""
        with self.status_lock:
            existing = self.status_by_user_id.get(user_id)
            if existing is not None and existing.is_running:
                return False
            self.status_by_user_id[user_id] = BackupTrimStatus(state=_RUNNING, detail="Starting backup cleanup...")
        thread = threading.Thread(
            target=self._run,
            kwargs={
                "user_id": user_id,
                "account_email": account_email,
                "cli": cli,
                "paths": paths,
                "notification_dispatcher": notification_dispatcher,
            },
            name=f"backup-trim-{user_id[:8]}",
            daemon=True,
        )
        thread.start()
        return True

    def _set_status(self, user_id: str, state: str, detail: str) -> None:
        with self.status_lock:
            self.status_by_user_id[user_id] = BackupTrimStatus(state=state, detail=detail)

    def _run(
        self,
        *,
        user_id: str,
        account_email: str,
        cli: ImbueCloudCli,
        paths: WorkspacePaths,
        notification_dispatcher: NotificationDispatcher | None,
    ) -> None:
        try:
            is_under_quota, detail = run_backup_trim(
                account_email=account_email,
                cli=cli,
                paths=paths,
                report_progress=lambda progress_detail: self._set_status(user_id, _RUNNING, progress_detail),
            )
            self._set_status(user_id, _SUCCEEDED if is_under_quota else _FAILED, detail)
        except (BackupProvisioningError, ImbueCloudCliError) as exc:
            logger.opt(exception=exc).warning("Backup trim for {} failed", account_email)
            self._set_status(user_id, _FAILED, f"Backup cleanup failed: {exc}")
        finally:
            # A crash from an unexpected exception type must not leave the
            # status stuck on "running" (the page would show a live cleanup
            # forever); flip it to failed before the thread dies.
            with self.status_lock:
                current = self.status_by_user_id.get(user_id)
                if current is not None and current.is_running:
                    self.status_by_user_id[user_id] = BackupTrimStatus(
                        state=_FAILED, detail="Backup cleanup stopped unexpectedly; see the logs."
                    )
        outcome = self.get_status(user_id)
        if notification_dispatcher is not None and outcome is not None:
            notification_dispatcher.dispatch(
                NotificationRequest(
                    title="Backup cleanup finished" if outcome.state == _SUCCEEDED else "Backup cleanup failed",
                    message=outcome.detail,
                    urgency=NotificationUrgency.NORMAL,
                ),
                agent_display_name=account_email,
            )
