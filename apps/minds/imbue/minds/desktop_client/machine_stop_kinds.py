"""Why a cloud machine is stopped, read from the connector (specs/workspace-stop-kinds.md).

Discovery tells the desktop a machine's lifecycle *state* (mngr's ``HostState``)
but not who asked for the stop or whether the owner may end it. That is the
connector's ``stop_kind``, which this module reads through the plugin's
``machines show`` listing rather than a new field on mngr's discovery models:
one round trip per signed-in account, only while some cloud machine is in a
connector-owned lifecycle state, at the discovery cadence and once at every
running-to-stopped edge.

The cached kinds serve the surfaces that show a machine's stop; the unattended
recovery asks for a *live* lifecycle read before starting a machine instead,
because discovery's cadence can lag the tracker's STUCK edge by most of a
poll, so a cached reading is not enough to tell "the machine wedged" from
"someone requested this stop".
"""

import threading
import time
from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import MachineSizeCliInfo
from imbue.minds.desktop_client.provider_display import is_imbue_cloud_provider_name
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStopKind

# The lifecycle states the connector owns: a machine in one of them is not
# running because someone asked for that, whatever the desktop's probes say.
CONNECTOR_OWNED_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    (HostState.STOPPING, HostState.STOPPED, HostState.STARTING)
)
CONNECTOR_OWNED_WORKSPACE_STATUSES: Final[frozenset[WorkspaceStatus]] = frozenset(
    (WorkspaceStatus.STOPPING, WorkspaceStatus.STOPPED, WorkspaceStatus.STARTING)
)
# Matches the imbue_cloud provider's default discovery cadence: the badge can
# never be fresher than the state it decorates.
DEFAULT_STOP_KIND_POLL_SECONDS: Final[float] = 30.0


class MachineStopKindTracker(MutableModel):
    """Keeps each stopped cloud machine's stop kind, read from the connector, for the UI and the recovery gate."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend_resolver: BackendResolverInterface = Field(
        frozen=True, description="Names the cloud machines and their lifecycle state (from discovery)."
    )
    session_store: MultiAccountSessionStore | None = Field(
        frozen=True, description="The signed-in accounts whose machines are listed; None lists nothing."
    )
    imbue_cloud_cli: ImbueCloudCli | None = Field(
        frozen=True, description="The plugin CLI the listing and the live read go through; None reads nothing."
    )
    poll_interval_seconds: float = Field(
        default=DEFAULT_STOP_KIND_POLL_SECONDS, frozen=True, description="Cadence of the background refresh."
    )
    on_change: Callable[[], None] | None = Field(
        default=None, description="Told whenever a refresh changed a kind (the UI publisher's notify_change)."
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop_kind_by_host_id: dict[str, str] = PrivateAttr(default_factory=dict)
    # The cloud hosts in a connector-owned state at the last pass whose
    # listings all succeeded: a wake re-lists only when this set changed (a
    # machine entered or left one), so a failed edge read is retried by the
    # next wake rather than waiting for the timed pass.
    _listed_host_ids: set[str] = PrivateAttr(default_factory=set)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _wake_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def stop_kind_by_host_id(self) -> dict[str, str]:
        """The last-read stop kind per host id (lowercase wire value; ``unknown`` for a kind this build lacks)."""
        with self._lock:
            return dict(self._stop_kind_by_host_id)

    def _cloud_host_ids_in_connector_owned_states(self) -> set[str]:
        host_ids: set[str] = set()
        for agent_id, host_state in self.backend_resolver.list_active_workspace_host_states().items():
            if host_state not in CONNECTOR_OWNED_HOST_STATES:
                continue
            info = self.backend_resolver.get_agent_display_info(agent_id)
            if info is not None and is_imbue_cloud_provider_name(info.provider_name or ""):
                host_ids.add(str(HostId(info.host_id)))
        return host_ids

    def refresh(self, *, is_only_on_state_change: bool = False) -> bool:
        """One pass: re-read the kinds when a cloud machine is stopped (or on its way), clear them otherwise.

        Returns whether anything changed. Every running machine means no
        listing at all: a stopped machine is the only thing a kind describes.
        ``is_only_on_state_change`` is the wake from a discovery change: it
        lists only when some cloud machine entered or left a connector-owned
        state since the last pass, so the frequent resolver events cost nothing
        while nothing moved; the timed pass always lists, which is what carries
        a kind changed server-side under an unchanged state.
        """
        wanted_host_ids = self._cloud_host_ids_in_connector_owned_states()
        with self._lock:
            if is_only_on_state_change and wanted_host_ids == self._listed_host_ids:
                return False
        fresh: dict[str, str] = {}
        is_any_listing_failed = False
        if wanted_host_ids and self.imbue_cloud_cli is not None and self.session_store is not None:
            accounts = self.session_store.list_accounts()
            # An account listing that failed names no accounts, so no machine
            # below gets listed either: the same "keep what was read" answer as
            # a failed machine listing, not an empty one.
            is_any_listing_failed = self.session_store.is_last_identity_read_failed
            for account in accounts:
                listing = self._list_machines_quietly(account.email)
                if listing is None:
                    is_any_listing_failed = True
                    continue
                for machine in listing:
                    if machine.host_id in wanted_host_ids and machine.stop_kind is not None:
                        fresh[machine.host_id] = _normalized_stop_kind(machine.stop_kind)
        with self._lock:
            if not is_any_listing_failed:
                self._listed_host_ids = wanted_host_ids
            else:
                # A failed listing names none of its account's machines; the
                # kinds it could not re-read stand rather than reading as "not
                # held" (which would offer Start on a machine an operator holds).
                for host_id in wanted_host_ids - fresh.keys():
                    previous = self._stop_kind_by_host_id.get(host_id)
                    if previous is not None:
                        fresh[host_id] = previous
            is_changed = fresh != self._stop_kind_by_host_id
            self._stop_kind_by_host_id = fresh
        if is_changed and self.on_change is not None:
            self.on_change()
        return is_changed

    def _list_machines_quietly(self, account_email: str) -> list[MachineSizeCliInfo] | None:
        """The account's machines, or None for a listing that failed (logged at debug)."""
        assert self.imbue_cloud_cli is not None
        try:
            return self.imbue_cloud_cli.list_machines(account_email)
        except ImbueCloudCliError as exc:
            logger.debug("Could not list {}'s machines for their stop kinds: {}", account_email, exc)
            return None

    def read_lifecycle(self, agent_id: AgentId) -> WorkspaceStatus | None:
        """The connector's current lifecycle status of a cloud workspace, read live; None when it cannot be read.

        The unattended recovery gate's question, asked at dispatch time rather
        than answered from discovery: is this machine down because someone
        asked for that?
        """
        if self.imbue_cloud_cli is None or self.session_store is None:
            return None
        info = self.backend_resolver.get_agent_display_info(agent_id)
        account = self.session_store.get_account_for_workspace(str(agent_id))
        if info is None or account is None or not is_imbue_cloud_provider_name(info.provider_name or ""):
            return None
        try:
            machine = self.imbue_cloud_cli.show_machine(account.email, info.host_id)
        except ImbueCloudCliError as exc:
            logger.debug("Could not read the lifecycle of {} from the connector: {}", agent_id, exc)
            return None
        return WorkspaceStatus(machine.status) if machine is not None else None

    def request_refresh(self) -> None:
        """Wake the background loop for an immediate pass; the resolver's on-change hook.

        Only signals (the resolver calls it on its own thread); the loop's pass
        lists when a cloud machine changed lifecycle state, so the badge follows
        a stop within the discovery cadence rather than a poll behind it.
        """
        self._wake_event.set()

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        concurrency_group.start_new_thread(target=self._run_loop, name="machine-stop-kinds", is_checked=False)

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def _run_loop(self) -> None:
        # The full pass runs on a deadline a wake does not move: the resolver
        # fires on every discovery snapshot, at the poll's own cadence, so a
        # wait restarted by each wake could keep a full pass (the one that
        # carries a kind changed server-side under an unchanged state) from
        # ever coming due.
        is_woken = False
        next_full_pass_at = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            is_full_pass = not is_woken or now >= next_full_pass_at
            if is_full_pass:
                next_full_pass_at = now + self.poll_interval_seconds
            try:
                self.refresh(is_only_on_state_change=not is_full_pass)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("The stop-kind refresh failed: {}", exc)
            is_woken = self._wake_event.wait(max(0.0, next_full_pass_at - time.monotonic()))
            self._wake_event.clear()


def _normalized_stop_kind(raw: str) -> str:
    """The wire value; a kind this build does not know reads ``unknown`` (shown but not actionable)."""
    return WorkspaceStopKind(raw).value
