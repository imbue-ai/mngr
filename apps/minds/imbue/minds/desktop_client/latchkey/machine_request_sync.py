"""Keeping this desktop's pending permission requests in step with the machines that filed them.

A remote workspace's agent files its permission requests with its own machine,
which forwards each to the user's desktops and keeps a record of it until the
user answers (see :mod:`imbue.mngr_latchkey.filed_permission_requests`). The
machine's records are therefore the truth about which of a remote host's
requests are still open, and this desktop's own gateway holds whatever it was
told while it was connected. The two drift apart in two ways, and this module
closes both:

* A request **answered here** is taken off the user's other desktops by asking
  the machine to forget it (:meth:`MachineRequestSync.forget_in_background`):
  the machine drops its record and sends the withdrawal to every connected
  desktop. Best-effort and off the request path, since the verdict has already
  been recorded and the agent told; a desktop the withdrawal did not reach
  syncs the request away itself.
* A desktop that was **offline or asleep** while requests were filed or
  answered catches up by syncing (:meth:`MachineRequestSync.start`): for every
  machine this desktop knows, the requests the machine keeps for this desktop
  and does not have here are filed on this desktop's gateway under the ids the
  machine gave them, and the ones this desktop has that the machine no longer
  keeps are dropped, since another desktop answered them. A request this
  desktop's gateway refuses to file is one no desktop will take, so the machine
  is asked to forget it. The pass runs when a machine first appears in
  discovery, and again for every machine at a fixed interval, so a laptop that
  wakes up catches up within it.

Everything that touches a machine goes through
:class:`~imbue.minds.desktop_client.latchkey.machine_access.MachineAccess`, so
a workspace whose agents run on this computer is left alone: its requests never
went through a machine. What this desktop files or drops goes through its own
gateway, whose follow stream then carries the change to every surface, so
nothing here signals the UI itself.
"""

import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import PermissionRequestRefusedError
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.machine_access import MACHINE_EXCHANGE_FAILURES
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.minds.desktop_client.latchkey.pending_requests import PendingRequestsInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.thread_cleanup import start_mngr_thread
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequest
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.store import permissions_path_for_host

# How often every known machine is synced, whatever discovery reported in
# between. A laptop that slept through a request being filed or answered
# catches up within this; the live path is the machine's withdrawal (the
# gateway's follow stream), not this.
_FULL_SYNC_INTERVAL_SECONDS: Final[float] = 600.0

# A request this desktop holds for a host and the host's machine does not keep
# is one another desktop answered -- unless it was filed moments ago, since the
# machine records a request only after the desktops have taken it. A request
# younger than this is left for the next pass to judge.
_FRESH_REQUEST_GRACE: Final[timedelta] = timedelta(minutes=2)

_SYNC_THREAD_NAME: Final[str] = "latchkey-machine-request-sync"
_FORGET_THREAD_NAME: Final[str] = "latchkey-machine-request-forget"

# Everything an exchange with a machine can fail with, plus this desktop's own
# gateway refusing an exchange. A sync is best-effort, so each is logged and
# the next pass tries again.
_SYNC_FAILURES: Final[tuple[type[Exception], ...]] = (*MACHINE_EXCHANGE_FAILURES, LatchkeyGatewayClientError)


class RequestSyncPlan(FrozenModel):
    """What one sync pass does for one machine, decided before anything is touched."""

    to_file_here: tuple[FiledPermissionRequest, ...] = Field(
        description="The machine's requests for this desktop that it does not hold yet: filed here under their ids."
    )
    to_drop_here: tuple[str, ...] = Field(
        description="Requests this desktop holds for the host that the machine no longer keeps: answered elsewhere."
    )
    to_forget_on_machine: tuple[str, ...] = Field(
        description="Requests the machine keeps that this desktop already answered: a withdrawal that did not reach it."
    )

    @property
    def is_empty(self) -> bool:
        return not (self.to_file_here or self.to_drop_here or self.to_forget_on_machine)


@pure
def plan_request_sync(
    filed_on_machine: Sequence[FiledPermissionRequest],
    pending_here_for_host: Sequence[StreamedPermissionRequest],
    resolved_here: frozenset[str],
    device_id: DesktopDeviceId,
    now: datetime,
) -> RequestSyncPlan:
    """Decide how this desktop's requests for one host and its machine's records are brought in step.

    ``pending_here_for_host`` is this desktop's pending requests that belong to
    the host, and ``resolved_here`` the ids of every request this desktop has
    recorded a verdict for. The machine's records are the truth, with two
    exceptions: a verdict recorded here outranks a record the machine still
    keeps (the withdrawal is what is missing, not the answer), and a request
    filed here within :data:`_FRESH_REQUEST_GRACE` is left alone, since the
    machine records a request only after the desktops have taken it.
    """
    filed_ids = {filed.request_id for filed in filed_on_machine}
    pending_ids = {pending.request_id for pending in pending_here_for_host}
    to_file_here: list[FiledPermissionRequest] = []
    to_forget_on_machine: list[str] = []
    for filed in filed_on_machine:
        if not filed.is_for_desktop(device_id):
            continue
        if filed.request_id in resolved_here:
            to_forget_on_machine.append(filed.request_id)
        elif filed.request_id in pending_ids:
            # Already held here, as it should be.
            continue
        else:
            to_file_here.append(filed)
    to_drop_here = tuple(
        pending.request_id
        for pending in pending_here_for_host
        if pending.request_id not in filed_ids and _is_old_enough_to_judge(pending, now)
    )
    return RequestSyncPlan(
        to_file_here=tuple(to_file_here),
        to_drop_here=to_drop_here,
        to_forget_on_machine=tuple(to_forget_on_machine),
    )


@pure
def _is_old_enough_to_judge(pending: StreamedPermissionRequest, now: datetime) -> bool:
    # A record from before the gateway stamped the filing time is old by definition.
    if pending.created_at is None:
        return True
    return now - pending.created_at.astimezone(timezone.utc) >= _FRESH_REQUEST_GRACE


class MachineRequestSync(MutableModel):
    """Carries verdicts given here to a remote host's machine, and brings this desktop's requests in step with it."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    access: MachineAccess = Field(frozen=True, description="Opens the machine behind a remote workspace.")
    gateway_client: LatchkeyGatewayClient = Field(
        frozen=True, description="This desktop's own gateway, where a synced request is filed or dropped."
    )
    pending_requests: PendingRequestsInterface = Field(
        frozen=True, description="What this desktop holds as pending, and every verdict it has recorded."
    )
    device_id: DesktopDeviceId = Field(
        frozen=True, description="This desktop, which a machine's record names among the desktops a request is for."
    )
    concurrency_group: ConcurrencyGroup = Field(
        frozen=True, description="Owns the sync thread and every forget thread; the app's root group."
    )
    now: Callable[[], datetime] = Field(
        default=lambda: datetime.now(timezone.utc),
        frozen=True,
        description="The clock a pass judges a request's age by (a seam for tests).",
    )

    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _wake_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: ObservableThread | None = PrivateAttr(default=None)
    _attempted_host_ids: set[HostId] = PrivateAttr(default_factory=set)

    def start(self) -> None:
        """Start the background sync: every known machine now, each new one as it appears, and all of them periodically."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = start_mngr_thread(
            self.concurrency_group,
            target=self._run,
            name=_SYNC_THREAD_NAME,
            daemon=True,
            # Best-effort: a failure here only means a desktop catches up on its
            # next start, and must never tear the app down.
            is_checked=False,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def notice_topology_change(self) -> None:
        """Wake the sync so a machine that has just appeared in discovery is synced without waiting for the interval."""
        self._wake_event.set()

    def forget_in_background(self, request_id: str, filing_agent_id: AgentId) -> None:
        """Ask the machine of the agent that filed ``request_id`` to forget it, off the caller's path.

        Nothing for a request whose agent runs on this computer, or on a machine
        this computer has not provisioned: neither went through a machine.
        """
        try:
            host_id = self.access.machine_host_for(str(filing_agent_id))
        except MachineUnreachableError as e:
            logger.warning("Could not tell which machine filed permission request {}: {}", request_id, e)
            return
        if host_id is None:
            return
        start_mngr_thread(
            self.concurrency_group,
            target=self._forget_on_machine,
            args=(str(filing_agent_id), host_id, request_id),
            name=_FORGET_THREAD_NAME,
            daemon=True,
            is_checked=False,
        )

    def sync_every_machine(self) -> None:
        """One pass over every machine this desktop knows, synchronously: the full pass the background thread runs at start and every interval."""
        self._sync_machines(is_every_machine=True)

    def _run(self) -> None:
        next_full_sync_at = time.monotonic()
        while not self._stop_event.is_set():
            # Cleared before the machines are listed, so a change that lands
            # during the pass wakes the next one rather than being lost. A stop
            # sets its flag before its wake, so a wake the clear just swallowed
            # is caught here rather than slept through.
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            is_full_pass = time.monotonic() >= next_full_sync_at
            self._sync_machines(is_every_machine=is_full_pass)
            if is_full_pass:
                next_full_sync_at = time.monotonic() + _FULL_SYNC_INTERVAL_SECONDS
            self._wake_event.wait(timeout=max(0.0, next_full_sync_at - time.monotonic()))

    def _sync_machines(self, is_every_machine: bool) -> None:
        """One pass: every machine this desktop knows, or only the ones never tried before.

        A wake comes with every change the backend reports, so between full
        passes only a machine never tried before is taken: one whose sync
        failed waits for the interval rather than being hammered.
        """
        for host_id, workspace_agent_id in self._workspace_agent_id_by_machine_host_id().items():
            if is_every_machine or host_id not in self._attempted_host_ids:
                self._attempted_host_ids.add(host_id)
                self._sync_machine(workspace_agent_id, host_id)

    def _workspace_agent_id_by_machine_host_id(self) -> dict[HostId, str]:
        """Every machine this desktop knows, each with one workspace it can be opened through."""
        workspace_agent_id_by_host_id: dict[HostId, str] = {}
        for workspace_agent_id in self.access.backend_resolver.list_active_workspace_ids():
            try:
                host_id = self.access.machine_host_for(str(workspace_agent_id))
            except MachineUnreachableError as e:
                logger.warning(
                    "Could not tell whether workspace {} has a machine of its own: {}", workspace_agent_id, e
                )
                continue
            if host_id is not None and host_id not in workspace_agent_id_by_host_id:
                workspace_agent_id_by_host_id[host_id] = str(workspace_agent_id)
        return workspace_agent_id_by_host_id

    def _sync_machine(self, workspace_agent_id: str, host_id: HostId) -> None:
        """Bring this desktop's requests for one host in step with its machine's records, in one exchange.

        A pass that fails is logged and left for the interval to try again.
        """
        host_permissions_path = permissions_path_for_host(self.access.latchkey.plugin_data_dir, host_id)
        try:
            with self.access.open_machine(workspace_agent_id, host_id) as machine:
                plan = plan_request_sync(
                    filed_on_machine=machine.list_filed_permission_requests(),
                    pending_here_for_host=self._pending_here_for(host_permissions_path),
                    resolved_here=frozenset(event.request_event_id for event in self.pending_requests.responses()),
                    device_id=self.device_id,
                    now=self.now(),
                )
                self._apply_plan(machine, plan, host_permissions_path)
        except _SYNC_FAILURES as e:
            logger.warning("Could not sync the permission requests of host {} with its machine: {}", host_id, e)
            return
        if not plan.is_empty:
            logger.info(
                "Synced the permission requests of host {} with its machine: filed {}, dropped {}, forgot {}",
                host_id,
                len(plan.to_file_here),
                len(plan.to_drop_here),
                len(plan.to_forget_on_machine),
            )

    def _pending_here_for(self, host_permissions_path: Path) -> tuple[StreamedPermissionRequest, ...]:
        """This desktop's pending requests that would be granted into the host's permissions file."""
        resolved_path = host_permissions_path.resolve()
        return tuple(
            pending
            for pending in self.pending_requests.list_pending()
            if Path(pending.target).resolve() == resolved_path
        )

    def _apply_plan(self, machine: MachineCredentials, plan: RequestSyncPlan, host_permissions_path: Path) -> None:
        for filed in plan.to_file_here:
            try:
                self.gateway_client.file_permission_request_as_filed_elsewhere(
                    filed.body, filed.request_id, host_permissions_path
                )
            except PermissionRequestRefusedError as e:
                # No desktop will ever take it, so it is not left for the next pass to find again.
                logger.warning("Dropping permission request {} from host {}: {}", filed.request_id, machine.host_id, e)
                machine.forget_permission_request(filed.request_id)
        for request_id in plan.to_forget_on_machine:
            machine.forget_permission_request(request_id)
        for request_id in plan.to_drop_here:
            self.gateway_client.delete_permission_request(request_id)

    def _forget_on_machine(self, workspace_agent_id: str, host_id: HostId, request_id: str) -> None:
        try:
            with self.access.open_machine(workspace_agent_id, host_id) as machine:
                machine.forget_permission_request(request_id)
        except _SYNC_FAILURES as e:
            logger.warning(
                "Could not withdraw permission request {} from the other desktops through the machine of host {}: {}",
                request_id,
                host_id,
                e,
            )
