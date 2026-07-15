"""Background scheduler for workspace-record sync.

Runs the reconcile (pull + legacy migration + metadata refresh + dirty pushes
+ definitively-absent tombstoning) event-driven: once after the session's
first complete discovery snapshot, then on a slow periodic tick, and
immediately whenever :meth:`WorkspaceSyncScheduler.kick` is called (sign-in /
sign-out, password changes). The legacy password-file conversion runs at the
start of every pass (a no-op once converted).

One daemon thread; every pass is wrapped so an expected failure (a connector
outage, or any sync / crypto / file error) can never kill the loop.
"""

import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.dek_store import convert_legacy_password_files
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import SyncCryptoError
from imbue.minds.errors import WorkspaceSyncError

# How often the loop re-reconciles when nothing kicks it. Records change
# rarely; this bounds how stale another device's view can get.
_SYNC_TICK_SECONDS = 60.0
# How often the loop re-checks for the first complete discovery snapshot
# before the first reconcile can run.
_DISCOVERY_POLL_SECONDS = 2.0


class InitialSyncState(UpperCaseStrEnum):
    """Progress of the first record fetch for a just-signed-in account."""

    PENDING = auto()
    FAILED = auto()
    DONE = auto()


class InitialSyncStatus(FrozenModel):
    """First-fetch progress for an account that signed in with no locally synced records.

    Backs the post-signin banner: the landing decision runs before the first
    record pull completes, so a returning user can land on the create form
    while their remote workspaces are still in flight. Tracking that window
    explicitly lets the UI say "fetching..." / "found N" instead of silently
    looking like the account has no workspaces.
    """

    user_id: str = Field(description="SuperTokens user id of the signed-in account")
    email: str = Field(description="Account email, for the banner text")
    state: InitialSyncState = Field(description="PENDING (fetch in flight), FAILED (last pass errored), or DONE")
    workspace_count: int = Field(default=0, description="Active synced workspaces found (DONE only)")
    error: str | None = Field(default=None, description="The last pass's failure description (FAILED only)")


class WorkspaceSyncScheduler(MutableModel):
    """Owns the background reconcile loop for workspace records."""

    record_store: WorkspaceRecordStore = Field(frozen=True, description="The sync engine the loop drives")
    session_store: MultiAccountSessionStore = Field(frozen=True, description="Source of the signed-in account list")
    resolver: BackendResolverInterface = Field(frozen=True, description="Discovery view records are built from")
    on_ssh_material_written: Callable[[], None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Invoked after a pass materializes new/changed SSH material, so the app "
            "can bounce discovery instead of waiting for the next poll"
        ),
    )
    _kick_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _initial_sync_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _initial_sync_by_user_id: dict[str, InitialSyncStatus] = PrivateAttr(default_factory=dict)

    def kick(self) -> None:
        """Request an immediate sync pass (sign-in/out, password change, association)."""
        self._kick_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._kick_event.set()

    def note_account_signin(self, user_id: str, email: str) -> None:
        """Track a just-signed-in account whose records are not local yet, then kick a pass.

        Accounts that already have locally synced records render their tiles
        instantly, so only a record-less account (fresh install, amnesia
        restore, or a brand-new account) gets a PENDING entry for the banner.
        """
        if not self.record_store.list_records(user_id):
            status = InitialSyncStatus(
                user_id=user_id,
                email=email,
                state=InitialSyncState.PENDING,
                workspace_count=0,
                error=None,
            )
            with self._initial_sync_lock:
                self._initial_sync_by_user_id[user_id] = status
        self.kick()

    def list_initial_sync_statuses(self) -> list[InitialSyncStatus]:
        with self._initial_sync_lock:
            return list(self._initial_sync_by_user_id.values())

    def run_one_pass(self) -> None:
        """One full sync pass: legacy conversion + reconcile for every signed-in account."""
        # Snapshot BEFORE the reconcile: an account marked mid-pass may not
        # have been included in it, and its signin kick guarantees the next
        # pass covers (and then resolves) it.
        with self._initial_sync_lock:
            tracked_user_ids = tuple(self._initial_sync_by_user_id.keys())
        accounts = {str(account.user_id): str(account.email) for account in self.session_store.list_accounts()}
        convert_legacy_password_files(self.record_store.paths, list(accounts.keys()))
        is_pull_ok_by_user_id = self.record_store.reconcile(accounts, self.resolver)
        self._resolve_initial_syncs(tracked_user_ids, accounts, is_pull_ok_by_user_id)
        # Materialize synced secrets (backup envs + cloud-row SSH material)
        # into their local consumers for every unlocked account. Compare-and-
        # write, so this self-heals deleted/corrupt files every pass.
        is_ssh_material_written = False
        for user_id, account_email in accounts.items():
            is_ssh_material_written = (
                self.record_store.materialize_account_synced_secrets(user_id, account_email) or is_ssh_material_written
            )
        if is_ssh_material_written and self.on_ssh_material_written is not None:
            self.on_ssh_material_written()

    def _resolve_initial_syncs(
        self,
        tracked_user_ids: Sequence[str],
        accounts: Mapping[str, str],
        is_pull_ok_by_user_id: Mapping[str, bool],
    ) -> None:
        """Settle tracked accounts after a pass: DONE on a successful pull, FAILED otherwise.

        The pull outcome matters because the record store deliberately
        swallows per-account pull failures (one unreachable account must not
        break the others): resolving on the pass alone would mark an account
        DONE with zero workspaces during a connector outage -- a false "you
        have no synced workspaces" on the very install the banner exists for.
        Entries for signed-out accounts are dropped; an entry that already
        reached DONE is never downgraded by a later failed pull.
        """
        for user_id in tracked_user_ids:
            if user_id not in accounts:
                with self._initial_sync_lock:
                    self._initial_sync_by_user_id.pop(user_id, None)
                continue
            if not is_pull_ok_by_user_id.get(user_id, False):
                self._mark_initial_sync_failed_unless_done(user_id, "could not fetch records from the sync service")
                continue
            active_count = sum(
                1 for record in self.record_store.list_records(user_id) if record.state == RECORD_STATE_ACTIVE
            )
            resolved_status = InitialSyncStatus(
                user_id=user_id,
                email=accounts[user_id],
                state=InitialSyncState.DONE,
                workspace_count=active_count,
                error=None,
            )
            with self._initial_sync_lock:
                self._initial_sync_by_user_id[user_id] = resolved_status

    def _mark_initial_sync_failed_unless_done(self, user_id: str, error: str) -> None:
        with self._initial_sync_lock:
            status = self._initial_sync_by_user_id.get(user_id)
            if status is not None and status.state != InitialSyncState.DONE:
                self._initial_sync_by_user_id[user_id] = status.model_copy_update(
                    to_update(status.field_ref().state, InitialSyncState.FAILED),
                    to_update(status.field_ref().error, error),
                )

    def _mark_pending_initial_syncs_failed(self, error: str) -> None:
        with self._initial_sync_lock:
            for user_id, status in self._initial_sync_by_user_id.items():
                if status.state == InitialSyncState.PENDING:
                    self._initial_sync_by_user_id[user_id] = status.model_copy_update(
                        to_update(status.field_ref().state, InitialSyncState.FAILED),
                        to_update(status.field_ref().error, error),
                    )

    def run_one_pass_guarded(self) -> None:
        """Run one pass; an expected failure is logged and marks tracked PENDING accounts FAILED.

        The loop is the only writer-side repair mechanism, so it must never
        die on a single bad pass (e.g. a connector outage) -- the next tick or
        kick retries, and a later successful pass flips FAILED back to DONE.
        """
        try:
            self.run_one_pass()
        except (ImbueCloudCliError, WorkspaceSyncError, SyncCryptoError, SecretWrappingError, OSError) as e:
            logger.opt(exception=e).error("Workspace-record sync pass failed")
            self._mark_pending_initial_syncs_failed(str(e))

    def _loop(self) -> None:
        # The first pass waits for a complete discovery snapshot so records
        # can be built with real metadata (names, providers, host ids).
        while not self._stop_event.is_set() and not self.resolver.has_completed_initial_discovery():
            self._stop_event.wait(_DISCOVERY_POLL_SECONDS)
        while not self._stop_event.is_set():
            # Clear BEFORE the pass: a kick arriving mid-pass then stays set,
            # so the wait below returns immediately and the request is served
            # by the next pass instead of being lost.
            self._kick_event.clear()
            self.run_one_pass_guarded()
            self._kick_event.wait(_SYNC_TICK_SECONDS)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the daemon loop on the app's root concurrency group."""
        concurrency_group.start_new_thread(
            target=self._loop,
            name="workspace-record-sync",
            daemon=True,
            is_checked=False,
        )
