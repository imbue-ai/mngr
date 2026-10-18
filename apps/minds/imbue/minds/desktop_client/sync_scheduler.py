"""Background scheduler for workspace-record and connection sync.

Runs the reconcile (pull + legacy migration + metadata refresh + dirty pushes
+ definitively-absent tombstoning) event-driven: once after the session's
first complete discovery snapshot, then on a slow periodic tick, and
immediately whenever :meth:`WorkspaceSyncScheduler.kick` is called (sign-in /
sign-out, password changes). The legacy password-file conversion runs at the
start of every pass (a no-op once converted). Each pass also pulls every
account's cloud connections, re-materializes the provider blocks, and retires accounts whose imbue_cloud session discovery
reports as expired (one forced refresh; a definitive refusal signs the
account out locally, a transient failure never does).

One daemon thread; every pass is wrapped so an expected failure (a connector
outage, or any sync / crypto / file error) can never kill the loop.
"""

import threading
import time
import tomllib
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from tomlkit.exceptions import TOMLKitError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.connections.account_lifecycle import has_live_create_on_account
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.provider_settings import ProviderSettingsMaterializer
from imbue.minds.desktop_client.dek_store import convert_legacy_password_files
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.imbue_cloud_cli import AuthRefreshOutcome
from imbue.minds.desktop_client.imbue_cloud_cli import IMBUE_CLOUD_AUTH_ERROR_CLASS
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import SyncCryptoError
from imbue.minds.errors import WorkspaceSyncError
from imbue.minds.mngr_settings.errors import MindsSettingsError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.primitives import ProviderInstanceName

# How often the loop re-reconciles when nothing kicks it. Records change
# rarely; this bounds how stale another device's view can get.
_SYNC_TICK_SECONDS = 60.0
# How often the loop re-checks for the first complete discovery snapshot
# before the first reconcile can run.
_DISCOVERY_POLL_SECONDS = 2.0
# How long :meth:`WorkspaceSyncScheduler.stop` waits for an in-flight pass to
# finish before giving up. Shutdown stops this loop before it tears down the
# shared mngr caller, so a pass that is mid-``mngr`` call must be allowed to
# complete (otherwise its next call would race the caller's teardown). Bounded
# so a wedged pass cannot stall shutdown indefinitely; it matches the root
# concurrency group's own shutdown budget.
_STOP_WAIT_TIMEOUT_SECONDS = 10.0
# Discovery error type names that mean an account instance's session no longer
# authenticates: the plugin's own auth error (what its discovery raises on an
# expired or revoked session) and mngr's not-authorized provider error.
SESSION_EXPIRED_ERROR_TYPE_NAMES: Final[frozenset[str]] = frozenset(
    {IMBUE_CLOUD_AUTH_ERROR_CLASS, ProviderNotAuthorizedError.__name__}
)
# A refresh that succeeded leaves the errored snapshot in place until the
# bounced observe child reports again, so the same account is not re-refreshed
# on every pass in between; the stamp is taken before the outcome is known, so
# it also spaces out retries after a transient refresh failure.
_SESSION_REFRESH_COOLDOWN_SECONDS: Final[float] = 600.0


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
    """Owns the background sync loop: each pass reconciles workspace records, pulls every
    account's cloud connections (re-materializing the provider blocks afterwards),
    and retires accounts whose imbue_cloud session discovery proves dead."""

    record_store: WorkspaceRecordStore = Field(frozen=True, description="The sync engine the loop drives")
    session_store: MultiAccountSessionStore = Field(frozen=True, description="Source of the signed-in account list")
    resolver: BackendResolverInterface = Field(frozen=True, description="Discovery view records are built from")
    on_discovery_bounce_needed: Callable[[], None] | None = Field(
        default=None,
        frozen=True,
        description=(
            "Invoked after a pass changes something a running discovery observer only reads at launch "
            "(new/changed SSH material, a refreshed account session), so the app can bounce it instead of "
            "waiting for the next poll"
        ),
    )
    backup_reaper: BackupReaperManager | None = Field(
        default=None,
        frozen=True,
        description=(
            "Reaps destroyed workspaces' backups past the retention window. Ticked at the "
            "end of every pass; it runs on its own (much longer) cadence internally"
        ),
    )
    forward_identity: ForwardIdentityPublisher | None = Field(
        default=None,
        frozen=True,
        description=(
            "Handed each pass's shared-workspace listing, so the proxy's request-headers file "
            "follows shares enabled or disabled from another device or the web chrome"
        ),
    )
    connection_store: ConnectionStore | None = Field(
        default=None, frozen=True, description="Cloud connection replicas pulled every pass; None disables"
    )
    provider_materializer: ProviderSettingsMaterializer | None = Field(
        default=None,
        frozen=True,
        description="Re-run on every pass, after the connection pulls; None disables",
    )
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        default=None, frozen=True, description="Used to force a session refresh when discovery reports auth errors"
    )
    agent_creator: AgentCreator | None = Field(
        default=None,
        frozen=True,
        description=(
            "Source of the creates in flight: an account with one on its providers is not retired, since "
            "the create's later mngr calls need the account's blocks; None never defers a retirement"
        ),
    )
    on_account_session_dead: Callable[[str], None] | None = Field(
        default=None,
        frozen=True,
        description="Invoked with a user id whose refresh the connector refused (the local signout)",
    )
    _last_session_refresh_at_by_user_id: dict[str, float] = PrivateAttr(default_factory=dict)
    _kick_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _exited_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _initial_sync_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _initial_sync_by_user_id: dict[str, InitialSyncStatus] = PrivateAttr(default_factory=dict)

    def kick(self) -> None:
        """Request an immediate sync pass (sign-in/out, password change, association)."""
        self._kick_event.set()

    def stop(self, wait_timeout_seconds: float = _STOP_WAIT_TIMEOUT_SECONDS) -> None:
        """Signal the loop to exit and block until the in-flight pass (if any) finishes.

        Setting ``_stop_event`` wakes the loop from its inter-pass wait, but it
        cannot interrupt a pass already running. Callers rely on this method to
        establish an ordering guarantee at shutdown: once it returns (barring a
        wedged pass that exceeds ``wait_timeout_seconds``), no ``run_one_pass``
        is in flight, so the shared mngr caller can be torn down without a pass
        racing it. Safe to call before :meth:`start` (the wait returns
        immediately since no loop has run).
        """
        self._stop_event.set()
        self._kick_event.set()
        if not self._exited_event.wait(timeout=wait_timeout_seconds):
            logger.warning("workspace-record sync loop did not stop within {}s", wait_timeout_seconds)

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
        # An account whose session discovery proves dead is retired before its
        # records and connections are synced: every later step would fail on
        # its auth anyway, and the retirement re-materializes the providers.
        for user_id in self._retire_accounts_with_dead_sessions(accounts):
            accounts.pop(user_id, None)
        convert_legacy_password_files(self.record_store.paths, list(accounts.keys()))
        outcome = self.record_store.reconcile(accounts, self.resolver)
        self._sync_connections(accounts)
        self._resolve_initial_syncs(tracked_user_ids, accounts, outcome.is_pull_ok_by_user_id)
        if self.forward_identity is not None:
            self.forward_identity.apply_sync_results(outcome.shared_agent_ids_by_user_id)
        # Materialize synced secrets (backup envs + cloud-row SSH material)
        # into their local consumers for every unlocked account. The SSH
        # application (key files + known_hosts pins, applied through the
        # host-key store) is gated on the record revision, with missing-file
        # escape hatches: a key pair or known_hosts file that has gone
        # missing is re-applied even when the revision has not advanced.
        is_ssh_material_written = False
        for user_id, account_email in accounts.items():
            is_ssh_material_written = (
                self.record_store.materialize_account_synced_secrets(user_id, account_email) or is_ssh_material_written
            )
        if is_ssh_material_written and self.on_discovery_bounce_needed is not None:
            self.on_discovery_bounce_needed()
        if self.backup_reaper is not None:
            self.backup_reaper.maybe_start_reap_pass(accounts)

    def _sync_connections(self, accounts: Mapping[str, str]) -> None:
        """Pull every account's connections, push dirty rows, and re-materialize the provider blocks.

        The materializer runs on every pass, not only when a pull changed a
        row: it lists the accounts itself, under its own lock, so a signout
        that happened during the pulls is honored rather than undone by a
        stale snapshot (and it is a no-op writer when nothing changed).
        """
        if self.connection_store is None:
            return
        for user_id, account_email in accounts.items():
            outcome = self.connection_store.pull(user_id, account_email)
            if outcome.is_reached:
                self.connection_store.push_dirty(user_id, account_email)
        if self.provider_materializer is not None:
            self.provider_materializer.materialize()

    def _retire_accounts_with_dead_sessions(self, accounts: Mapping[str, str]) -> list[str]:
        """Sign out (locally) every account whose account instance discovery reports as unauthenticated
        and whose forced refresh the connector refuses; returns the retired user ids.

        A transient refresh failure leaves the account active: only the plugin's
        own auth error proves the session dead. A successful refresh bounces
        discovery so the errored snapshot is replaced. An account with a
        workspace create in flight on one of its providers is skipped (not
        even refreshed); the refresh cooldown retries it on a later pass.
        """
        if self.imbue_cloud_cli is None:
            return []
        errors = self.resolver.get_provider_errors()
        retired: list[str] = []
        is_any_refreshed = False
        now = time.monotonic()
        for user_id, account_email in accounts.items():
            error = errors.get(ProviderInstanceName(imbue_cloud_provider_name_for_user(user_id)))
            if error is None or error.type_name not in SESSION_EXPIRED_ERROR_TYPE_NAMES:
                continue
            last_refresh_at = self._last_session_refresh_at_by_user_id.get(user_id)
            if last_refresh_at is not None and now - last_refresh_at < _SESSION_REFRESH_COOLDOWN_SECONDS:
                continue
            if self._has_live_create_on_account(user_id):
                logger.info(
                    "Leaving the expired-looking session of {} alone: a workspace is still being created on "
                    "one of the account's providers",
                    account_email,
                )
                continue
            self._last_session_refresh_at_by_user_id[user_id] = now
            outcome = self.imbue_cloud_cli.auth_refresh_outcome(account_email)
            match outcome:
                case AuthRefreshOutcome.REFRESHED:
                    logger.info(
                        "Refreshed the expired-looking session of {} after a discovery auth error", account_email
                    )
                    is_any_refreshed = True
                case AuthRefreshOutcome.SESSION_DEAD:
                    logger.warning(
                        "The session of {} is dead (refresh refused); signing the account out", account_email
                    )
                    if self.on_account_session_dead is not None:
                        self.on_account_session_dead(user_id)
                    # A re-signin within the cooldown starts a fresh session,
                    # which deserves its own refresh on the next auth error.
                    self._last_session_refresh_at_by_user_id.pop(user_id, None)
                    retired.append(user_id)
                case AuthRefreshOutcome.TRANSIENT_FAILURE:
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        if is_any_refreshed and self.on_discovery_bounce_needed is not None:
            self.on_discovery_bounce_needed()
        return retired

    def _has_live_create_on_account(self, user_id: str) -> bool:
        if self.agent_creator is None:
            return False
        return has_live_create_on_account(
            user_id,
            self.connection_store,
            self.agent_creator.live_in_flight_provider_instance_names(),
            self.agent_creator.live_in_flight_account_ids(),
        )

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
        except (
            ImbueCloudCliError,
            WorkspaceSyncError,
            SyncCryptoError,
            SecretWrappingError,
            OSError,
            # The pass re-materializes the provider settings, which a malformed env
            # override or a hand-broken settings.toml makes fail until it is fixed.
            MindsSettingsError,
            tomllib.TOMLDecodeError,
            TOMLKitError,
        ) as e:
            logger.opt(exception=e).error("Machine-record sync pass failed")
            self._mark_pending_initial_syncs_failed(str(e))

    def _loop(self) -> None:
        # ``_exited_event`` is what :meth:`stop` blocks on, so it must be set on
        # every exit path (clean stop or an unexpected escape) -- otherwise a
        # shutdown would wait out the full timeout for a loop that is gone.
        try:
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
        finally:
            self._exited_event.set()

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the daemon loop on the app's root concurrency group."""
        self._exited_event.clear()
        concurrency_group.start_new_thread(
            target=self._loop,
            name="workspace-record-sync",
            daemon=True,
            is_checked=False,
        )
