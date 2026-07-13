"""Background scheduler for workspace-record sync.

Runs the reconcile (pull + legacy migration + metadata refresh + dirty pushes
+ definitively-absent tombstoning) event-driven: once after the session's
first complete discovery snapshot, then on a slow periodic tick, and
immediately whenever :meth:`WorkspaceSyncScheduler.kick` is called (sign-in /
sign-out, password changes). The legacy password-file conversion runs at the
start of every pass (a no-op once converted).

One daemon thread; every pass is wrapped so a connector outage or a
mid-refactor exception can never kill the loop.
"""

import threading

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.dek_store import convert_legacy_password_files
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.minds.errors import SyncCryptoError
from imbue.minds.errors import WorkspaceSyncError

# How often the loop re-reconciles when nothing kicks it. Records change
# rarely; this bounds how stale another device's view can get.
_SYNC_TICK_SECONDS = 60.0
# How often the loop re-checks for the first complete discovery snapshot
# before the first reconcile can run.
_DISCOVERY_POLL_SECONDS = 2.0


class WorkspaceSyncScheduler(MutableModel):
    """Owns the background reconcile loop for workspace records."""

    record_store: WorkspaceRecordStore = Field(frozen=True, description="The sync engine the loop drives")
    session_store: MultiAccountSessionStore = Field(frozen=True, description="Source of the signed-in account list")
    resolver: BackendResolverInterface = Field(frozen=True, description="Discovery view records are built from")
    _kick_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def kick(self) -> None:
        """Request an immediate sync pass (sign-in/out, password change, association)."""
        self._kick_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._kick_event.set()

    def run_one_pass(self) -> None:
        """One full sync pass: legacy conversion + reconcile for every signed-in account."""
        accounts = {str(account.user_id): str(account.email) for account in self.session_store.list_accounts()}
        convert_legacy_password_files(self.record_store.paths, list(accounts.keys()))
        self.record_store.reconcile(accounts, self.resolver)

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
            try:
                self.run_one_pass()
            except (ImbueCloudCliError, WorkspaceSyncError, SyncCryptoError, OSError) as e:
                # The loop is the only writer-side repair mechanism; log loudly
                # but never die on a single bad pass (e.g. a connector outage).
                logger.opt(exception=e).error("Workspace-record sync pass failed")
            self._kick_event.wait(_SYNC_TICK_SECONDS)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        """Start the daemon loop on the app's root concurrency group."""
        concurrency_group.start_new_thread(
            target=self._loop,
            name="workspace-record-sync",
            daemon=True,
            is_checked=False,
        )
