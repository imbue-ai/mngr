"""Move legacy bucket-backed workspace backups into the backup store.

Before the backup store, every Imbue Cloud backup lived in its own R2 bucket
reached over S3 with a per-bucket credential. The migration (``specs/
backup-store/spec.md``, section 5) re-provisions each such workspace into
the store through the ordinary destination-change path: the old canonical
``restic.env`` is archived (its repository stays readable as "previous
storage"), a store repository is created, the new env is injected, and the
old bucket's credential is then frozen read-only so nothing can write to a
repository the desktop considers retired.

The landing page's backup status batch drives it: every pass offers the
workspaces it rendered, and the manager starts at most a few migrations per
pass, online workspaces only, each as a tracked workspace operation so the
UI shows it and nothing else runs on that workspace meanwhile.

CLEANUP: delete this module (and its call from the backups stream route)
once the backup-store migration's legacy reap has run on production and no
canonical env names an S3 bucket.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroupError
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import archived_password_for_repository
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import split_backup_bucket_name_from_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_reaper import make_quota_evictor
from imbue.minds.desktop_client.backup_reaper import user_id_prefix_for
from imbue.minds.desktop_client.backup_update import BackupWorkerFailureHandler
from imbue.minds.desktop_client.backup_verification import is_workspace_online
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryInfo
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.thread_cleanup import start_mngr_thread

# At most this many migrations start per status batch, so a fleet of legacy
# workspaces drains over a few passes without saturating the connector or
# the workspaces' upload bandwidth all at once.
MAX_MIGRATIONS_PER_PASS: Final[int] = 2
# A failed migration (a store outage, a workspace that went offline mid-way)
# is retried no sooner than this.
_RETRY_INTERVAL_SECONDS: Final[float] = 60.0 * 60.0


def is_legacy_backup_env(env_content: str, owner_prefix: str) -> bool:
    """Whether a canonical env still points at one of the account's legacy per-workspace buckets.

    Only a bucket the connector made for this account (``<owner_prefix>--<name>``
    on R2) is a migration candidate: a bring-your-own repository that happens to
    live on R2 belongs to the user and is never moved.
    """
    legacy_bucket = split_backup_bucket_name_from_env(env_content)
    return legacy_bucket is not None and legacy_bucket[0] == owner_prefix


def _is_migrated_by_another_device(
    paths: InstallationPaths, agent_id: AgentId, store_repositories: Sequence[BackupRepositoryInfo]
) -> bool:
    """Whether the store already holds the workspace's repository and this device did not create it.

    Every device of the account sees the same online workspace with the same
    legacy env, so two desktops can pick it up on the same pass. A repository
    none of this device's archived envs names was created by another device
    (its migration is under way or done); one an archive names is this
    device's own rolled-back attempt, which the retry resumes with the
    archived password.
    """
    for repository in store_repositories:
        if repository.workspace_id != str(agent_id):
            continue
        return archived_password_for_repository(paths, agent_id, repository.repository_url) is None
    return False


class BackupStoreMigrationManager(MutableModel):
    """Starts legacy-to-store backup migrations, a few per status batch, one per workspace at a time."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    change_destination_fn: Callable[..., None] = Field(
        default=change_backup_destination_for_host,
        frozen=True,
        description="Runs one workspace's destination change (the real provisioning; a fake in tests)",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _attempted_at_by_agent_id: dict[AgentId, float] = PrivateAttr(default_factory=dict)
    _in_flight: set[AgentId] = PrivateAttr(default_factory=set)

    def maybe_start_migrations(
        self,
        agent_ids: Sequence[AgentId],
        *,
        paths: InstallationPaths,
        imbue_cloud_cli: ImbueCloudCli | None,
        resolver: BackendResolverInterface,
        registry: WorkspaceOperationRegistryInterface,
        session_store: MultiAccountSessionStore | None,
        record_store: WorkspaceRecordStore | None,
        parent_cg: ConcurrencyGroup | None,
    ) -> list[AgentId]:
        """Start migrations for up to :data:`MAX_MIGRATIONS_PER_PASS` eligible workspaces; returns the ones started.

        Eligible: the canonical env names a legacy bucket, the workspace
        belongs to a signed-in Imbue Cloud account, it is online, the store
        holds no repository for it that another device created, no migration
        of it is in flight or was attempted within the retry interval, and no
        other tracked operation runs on it.
        """
        if imbue_cloud_cli is None or session_store is None or parent_cg is None:
            return []
        started: list[AgentId] = []
        store_repositories_by_email: dict[str, list[BackupRepositoryInfo]] = {}
        for agent_id in agent_ids:
            if len(started) >= MAX_MIGRATIONS_PER_PASS:
                break
            if not self._claim(agent_id):
                continue
            account = session_store.get_account_for_workspace(str(agent_id))
            env_content = read_canonical_env(paths, agent_id)
            if (
                account is None
                or env_content is None
                or not is_legacy_backup_env(env_content, user_id_prefix_for(str(account.user_id)))
            ):
                self._release(agent_id, is_attempted=False)
                continue
            if not is_workspace_online(resolver, agent_id):
                self._release(agent_id, is_attempted=False)
                continue
            account_email = str(account.email)
            try:
                if account_email not in store_repositories_by_email:
                    store_repositories_by_email[account_email] = imbue_cloud_cli.list_backup_repositories(
                        account_email
                    )
            except ImbueCloudCliError as exc:
                logger.warning("Could not list the backup store's repositories before migrating {}: {}", agent_id, exc)
                self._release(agent_id, is_attempted=True)
                continue
            if _is_migrated_by_another_device(paths, agent_id, store_repositories_by_email[account_email]):
                # The record sync delivers that device's env; the next pass sees it.
                self._release(agent_id, is_attempted=False)
                continue
            if not registry.start_if_idle(
                agent_id, WorkspaceOperationKind.BACKUP_CONFIGURE, datetime.now(timezone.utc), None
            ):
                self._release(agent_id, is_attempted=False)
                continue
            quota_evictor = (
                make_quota_evictor(
                    record_store=record_store,
                    paths=paths,
                    imbue_cloud_cli=imbue_cloud_cli,
                    user_id=str(account.user_id),
                    account_email=str(account.email),
                )
                if record_store is not None
                else None
            )
            try:
                start_mngr_thread(
                    parent_cg,
                    target=self._run_migration,
                    kwargs={
                        "agent_id": agent_id,
                        "agent_address": build_agent_address(agent_id, resolver),
                        "account_email": account_email,
                        "legacy_env_content": env_content,
                        "paths": paths,
                        "imbue_cloud_cli": imbue_cloud_cli,
                        "registry": registry,
                        "parent_cg": parent_cg,
                        "quota_evictor": quota_evictor,
                    },
                    name=f"backup-store-migration-{agent_id}",
                    daemon=True,
                    is_checked=False,
                    on_failure=BackupWorkerFailureHandler(workspace_agent_id=agent_id, registry=registry),
                )
            except (OSError, RuntimeError, ConcurrencyGroupError) as exc:
                logger.warning("Could not start the backup-store migration worker for {}: {}", agent_id, exc)
                registry.fail(agent_id, f"Could not start the backup migration: {exc}")
                self._release(agent_id, is_attempted=True)
                continue
            started.append(agent_id)
        return started

    def _claim(self, agent_id: AgentId) -> bool:
        now = time.monotonic()
        with self._lock:
            if agent_id in self._in_flight:
                return False
            attempted_at = self._attempted_at_by_agent_id.get(agent_id)
            if attempted_at is not None and now - attempted_at < _RETRY_INTERVAL_SECONDS:
                return False
            self._in_flight.add(agent_id)
        return True

    def _release(self, agent_id: AgentId, *, is_attempted: bool) -> None:
        with self._lock:
            self._in_flight.discard(agent_id)
            if is_attempted:
                self._attempted_at_by_agent_id[agent_id] = time.monotonic()

    def _run_migration(
        self,
        *,
        agent_id: AgentId,
        agent_address: str,
        account_email: str,
        legacy_env_content: str,
        paths: InstallationPaths,
        imbue_cloud_cli: ImbueCloudCli,
        registry: WorkspaceOperationRegistryInterface,
        parent_cg: ConcurrencyGroup,
        quota_evictor: Callable[[], bool] | None,
    ) -> None:
        try:
            run_backup_store_migration(
                agent_id=agent_id,
                agent_address=agent_address,
                account_email=account_email,
                legacy_env_content=legacy_env_content,
                paths=paths,
                imbue_cloud_cli=imbue_cloud_cli,
                registry=registry,
                parent_cg=parent_cg,
                quota_evictor=quota_evictor,
                change_destination_fn=self.change_destination_fn,
            )
        finally:
            self._release(agent_id, is_attempted=True)


def run_backup_store_migration(
    *,
    agent_id: AgentId,
    agent_address: str,
    account_email: str,
    legacy_env_content: str,
    paths: InstallationPaths,
    imbue_cloud_cli: ImbueCloudCli,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    quota_evictor: Callable[[], bool] | None,
    # Injectable (a fake in tests); production passes the real destination change.
    change_destination_fn: Callable[..., None] = change_backup_destination_for_host,
) -> None:
    """Migrate one workspace (the caller has registered the operation); ends it via the registry.

    The destination change archives the legacy canonical env, provisions a
    store repository, and injects the new env; only once that has succeeded
    is the legacy bucket's credential frozen read-only. A freeze failure is
    logged, never fatal: the next legacy reap freezes or deletes the bucket.
    """
    registry.append_log(agent_id, "Moving backups to the new backup storage...")
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email=account_email)
    try:
        change_destination_fn(
            agent_id=agent_id,
            agent_address=agent_address,
            request=request,
            imbue_cloud_cli=imbue_cloud_cli,
            paths=paths,
            parent_cg=parent_cg,
            quota_evictor=quota_evictor,
            # A change that failed at injection must not leave the store env
            # canonical: the next pass would skip the workspace as migrated
            # while its backups still go to the legacy bucket.
            is_rolled_back_on_failure=True,
        )
    except (BackupProvisioningError, ImbueCloudCliError) as exc:
        logger.warning("Backup-store migration for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, f"Could not move the backups to the new storage: {exc}")
        return
    legacy_bucket = split_backup_bucket_name_from_env(legacy_env_content)
    if legacy_bucket is not None:
        registry.append_log(agent_id, "Making the previous backup storage read-only...")
        try:
            imbue_cloud_cli.freeze_bucket(account_email, legacy_bucket[1])
        except ImbueCloudCliError as exc:
            logger.warning("Could not freeze the legacy backup bucket of {}: {}", agent_id, exc)
    registry.append_log(agent_id, "Backups now go to the new storage; the first backup there runs within the hour.")
    registry.complete(agent_id)
