"""The app's own restart of a workspace's machine onto the fixed kernel.

One press runs the whole sequence in the background: start the machine if the
press found it stopped, install the kernel package where it is still below the
floor (which disturbs nothing running), stop the workspace's host, bring the
machine back on the new kernel, start the host, and read the kernel it booted.
The badge clears only when that read shows a new boot on a kernel at or above
the floor; a machine that did not come back is reported as such, never as done.

Which step boots the new kernel is the provider's (``KernelRestartMethod``).
A Lima VM comes back on it through the host stop and start themselves
(``limactl`` halts and boots the VM), and so does a cloud instance on AWS, GCP
or Azure, which the host stop pauses whole and the start resumes. A Vultr VPS
keeps running through its container's stop, so it is rebooted in place through
a transient systemd timer over the outer host, and the start waits until the
VPS answers with a new boot id.

A restart ends every chat's process, so a workspace with a chat running or busy,
or whose chats cannot be listed, is refused unless the press says to go ahead
anyway; so is one with a template update in flight.
"""

import time
from collections.abc import Callable
from enum import auto
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.kernel_security import KERNEL_INSTALL_TIMEOUT_SECONDS
from imbue.minds.desktop_client.kernel_security import KERNEL_REBOOT_SCHEDULE_TIMEOUT_SECONDS
from imbue.minds.desktop_client.kernel_security import KERNEL_SECURITY_FLOOR_PACKAGE_VERSION
from imbue.minds.desktop_client.kernel_security import KernelMachineAccess
from imbue.minds.desktop_client.kernel_security import KernelProbe
from imbue.minds.desktop_client.kernel_security import KernelProbeTransport
from imbue.minds.desktop_client.kernel_security import KernelRestartMethod
from imbue.minds.desktop_client.kernel_security import KernelScriptAnswer
from imbue.minds.desktop_client.kernel_security import KernelScriptStatus
from imbue.minds.desktop_client.kernel_security import read_kernel_probe
from imbue.minds.desktop_client.kernel_security import restarted_machine_shortfall
from imbue.minds.desktop_client.kernel_security import run_machine_script
from imbue.minds.desktop_client.update_apply_window import active_chat_agent_names
from imbue.minds.desktop_client.update_apply_window import build_update_run_probe_args
from imbue.minds.desktop_client.update_status import KernelFixActivity
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.thread_cleanup import start_mngr_thread

# The chat listing is an in-workspace ``mngr list`` plus the chat app's busy read.
_CHAT_PROBE_TIMEOUT_SECONDS: Final[float] = 120.0
# How long a rebooted VPS has to answer the probe with a new boot id.
_REBOOT_TIMEOUT_SECONDS: Final[float] = 600.0
_REBOOT_POLL_INTERVAL_SECONDS: Final[float] = 5.0
# How long a restarted machine has to answer the kernel probe once its host is up again.
_VERIFICATION_TIMEOUT_SECONDS: Final[float] = 120.0


class KernelFixDispatchOutcome(UpperCaseStrEnum):
    """What a press of the restart came to."""

    DISPATCHED = auto()
    NOT_OFFERED = auto()
    """The machine has no fix on offer: it is fixed, unread, or not one the app can restart."""
    ALREADY_RUNNING = auto()
    UPDATE_IN_FLIGHT = auto()
    """A template update is running; the restart would end its agent mid-run."""
    CHATS_RUNNING = auto()
    """A chat is running or busy; the press may say to go ahead anyway."""
    CHATS_UNREADABLE = auto()
    """The chats could not be listed, so a running one cannot be ruled out; the press may say to go ahead anyway."""


KERNEL_FIX_REFUSAL_MESSAGE_BY_OUTCOME: Final[dict[KernelFixDispatchOutcome, str]] = {
    KernelFixDispatchOutcome.NOT_OFFERED: "This workspace's machine has no kernel update to apply.",
    KernelFixDispatchOutcome.ALREADY_RUNNING: "This workspace's machine is already restarting onto the new kernel.",
    KernelFixDispatchOutcome.UPDATE_IN_FLIGHT: (
        "An update is running in this workspace. Restart the machine once it finishes."
    ),
    KernelFixDispatchOutcome.CHATS_RUNNING: "A chat is still working in this workspace; restarting now would interrupt it.",
    KernelFixDispatchOutcome.CHATS_UNREADABLE: (
        "Couldn't tell whether a chat is working in this workspace; restarting now could interrupt one."
    ),
}

# The refusals a repeated press with "go ahead anyway" gets past.
FORCEABLE_KERNEL_FIX_OUTCOMES: Final[frozenset[KernelFixDispatchOutcome]] = frozenset(
    {KernelFixDispatchOutcome.CHATS_RUNNING, KernelFixDispatchOutcome.CHATS_UNREADABLE}
)


class KernelFixDispatch(FrozenModel):
    """What a press came to, with the chats that stood in its way when they did."""

    outcome: KernelFixDispatchOutcome = Field(description="Dispatched, or why not")
    running_chats: tuple[str, ...] = Field(
        default=(), description="The chats a restart would interrupt, by agent name"
    )


class KernelFixRunner(MutableModel):
    """Runs the restart onto the fixed kernel for one workspace at a time each, in the background."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    state_store: WorkspaceUpdateStateStore = Field(
        frozen=True, description="Where the restart's progress is published."
    )
    mngr_caller: MngrCaller = Field(frozen=True, description="Runs the machine script and the chat listing.")
    backend_resolver: BackendResolverInterface = Field(frozen=True, description="Places each workspace's host.")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Parent group for the restart worker.")
    kernel_access_for: Callable[[AgentId], KernelMachineAccess | None] = Field(
        frozen=True,
        description="How a workspace's machine is reached as root and restarted; None for one the app does not restart",
    )
    stop_workspace: Callable[[AgentId], MindHostActionOutcome] = Field(
        frozen=True,
        description="The shared host stop: marks the machine as stopped on purpose, so unattended recovery leaves "
        "it alone while it is down, and closes the windows open to it",
    )
    start_workspace: Callable[[AgentId], MindHostActionOutcome] = Field(
        frozen=True, description="The shared host start, which clears that mark once the machine is back"
    )
    on_fix_ended: Callable[[AgentId], None] = Field(
        frozen=True, description="Told when a restart ends, so the sweep re-reads the machine"
    )
    floor_package_version: str = Field(
        default=KERNEL_SECURITY_FLOOR_PACKAGE_VERSION, description="The kernel package version to install up to"
    )
    reboot_timeout_seconds: float = Field(default=_REBOOT_TIMEOUT_SECONDS)
    reboot_poll_interval_seconds: float = Field(default=_REBOOT_POLL_INTERVAL_SECONDS)
    verification_timeout_seconds: float = Field(default=_VERIFICATION_TIMEOUT_SECONDS)

    def dispatch(self, agent_id: AgentId, *, is_forced: bool) -> KernelFixDispatch:
        """Start the restart for ``agent_id`` in the background, or say why not.

        The chat listing is read before the claim, so a refused press leaves the
        row untouched; the claim itself is atomic, so two presses cannot both run.
        """
        state = self.state_store.get(agent_id)
        if not state.kernel.is_fix_offered:
            return KernelFixDispatch(outcome=KernelFixDispatchOutcome.NOT_OFFERED)
        if state.is_kernel_fix_in_flight:
            return KernelFixDispatch(outcome=KernelFixDispatchOutcome.ALREADY_RUNNING)
        if state.is_run_in_flight:
            return KernelFixDispatch(outcome=KernelFixDispatchOutcome.UPDATE_IN_FLIGHT)
        if not is_forced:
            running_chats = self._running_chats(agent_id)
            if running_chats is None:
                return KernelFixDispatch(outcome=KernelFixDispatchOutcome.CHATS_UNREADABLE)
            if running_chats:
                return KernelFixDispatch(
                    outcome=KernelFixDispatchOutcome.CHATS_RUNNING, running_chats=tuple(running_chats)
                )
        if not self.state_store.try_begin_kernel_fix(agent_id):
            is_run_in_flight = self.state_store.get(agent_id).is_run_in_flight
            return KernelFixDispatch(
                outcome=KernelFixDispatchOutcome.UPDATE_IN_FLIGHT
                if is_run_in_flight
                else KernelFixDispatchOutcome.ALREADY_RUNNING
            )
        try:
            start_mngr_thread(
                self.concurrency_group,
                target=self._run,
                args=(agent_id,),
                name=f"kernel-fix-{agent_id}",
                daemon=True,
                is_checked=False,
                on_failure=lambda exc: self._end(
                    agent_id, probe=None, failure=f"The restart stopped unexpectedly: {exc}"
                ),
            )
        except (OSError, RuntimeError, ConcurrencyGroupError, ConcurrencyExceptionGroup) as exc:
            logger.opt(exception=exc).error("Failed to spawn the kernel restart worker for {}: {}", agent_id, exc)
            self._end(agent_id, probe=None, failure=f"Couldn't start the restart: {exc}")
        return KernelFixDispatch(outcome=KernelFixDispatchOutcome.DISPATCHED)

    def _running_chats(self, agent_id: AgentId) -> list[str] | None:
        """The chats a restart would interrupt, by agent name; None when they could not be listed."""
        display = self.backend_resolver.get_agent_display_info(agent_id)
        excluded = frozenset({str(agent_id)} | ({display.agent_name} if display is not None else set()))
        result = self.mngr_caller.call(
            build_update_run_probe_args(build_agent_address(agent_id, self.backend_resolver)),
            timeout=_CHAT_PROBE_TIMEOUT_SECONDS,
        )
        if result.is_timed_out:
            return None
        return active_chat_agent_names(result.stdout, excluded)

    def _run(self, agent_id: AgentId) -> None:
        access = self.kernel_access_for(agent_id)
        if access is None:
            self._end(agent_id, probe=None, failure="This machine is not one Imbue Studio can restart.")
            return
        transport = access.transport
        address = build_agent_address(agent_id, self.backend_resolver)
        logger.info("Installing the fixed kernel in the machine of {}", agent_id)
        install = self._install_kernel(transport, address)
        # A machine the press found stopped is started for the install, as the template dispatch
        # starts one for its command: the restart brings it up anyway, and the press is the user's word.
        if not install.is_machine_reached:
            start = self.start_workspace(agent_id)
            if not start.is_successful:
                self._end(agent_id, probe=None, failure=f"Couldn't start the machine: {start.failure_reason}")
                return
            install = self._install_kernel(transport, address)
        if install.status is not KernelScriptStatus.INSTALLED or install.probe is None:
            self._end(agent_id, probe=install.probe, failure=f"Couldn't install the kernel package: {install.detail}")
            return
        previous_boot_id = install.probe.boot_id
        self.state_store.set_kernel_fix_activity(agent_id, KernelFixActivity.RESTARTING)
        restart_failure = self._bring_machine_back(agent_id, access, address, previous_boot_id)
        if restart_failure is not None:
            self._end(agent_id, probe=install.probe, failure=restart_failure)
            return
        self.state_store.set_kernel_fix_activity(agent_id, KernelFixActivity.VERIFYING)
        probe = self._probe_until_answered(transport, address, self.verification_timeout_seconds)
        if probe is None:
            self._end(agent_id, probe=None, failure="The machine came back, but its kernel couldn't be read.")
            return
        shortfall = restarted_machine_shortfall(probe, self.floor_package_version, previous_boot_id)
        self._end(agent_id, probe=probe, failure=shortfall if shortfall is not None else "")

    def _install_kernel(self, transport: KernelProbeTransport, address: str) -> KernelScriptAnswer:
        return run_machine_script(
            self.mngr_caller,
            transport,
            address,
            "install",
            floor_package_version=self.floor_package_version,
            timeout_seconds=KERNEL_INSTALL_TIMEOUT_SECONDS,
        )

    def _bring_machine_back(
        self, agent_id: AgentId, access: KernelMachineAccess, address: str, previous_boot_id: str
    ) -> str | None:
        """Take the host down and bring the machine back on the installed kernel, the provider's way; why it did not, or None."""
        logger.info("Stopping the machine of {} to restart it onto the fixed kernel", agent_id)
        stop = self.stop_workspace(agent_id)
        if not stop.is_successful:
            return f"Couldn't stop the machine: {stop.failure_reason}"
        match access.restart:
            case KernelRestartMethod.IN_PLACE_REBOOT:
                reboot_failure = self._reboot_machine_in_place(access.transport, address, previous_boot_id)
                if reboot_failure is not None:
                    # The container was stopped for a reboot that did not happen; bring it back rather than leave it down.
                    restart = self.start_workspace(agent_id)
                    if not restart.is_successful:
                        reboot_failure += f" The workspace could not be started again either: {restart.failure_reason}"
                    return reboot_failure
            case KernelRestartMethod.HOST_CYCLE:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        logger.info("Starting the machine of {} again", agent_id)
        start = self.start_workspace(agent_id)
        if not start.is_successful:
            return f"Couldn't start the machine again: {start.failure_reason}"
        return None

    def _reboot_machine_in_place(
        self, transport: KernelProbeTransport, address: str, previous_boot_id: str
    ) -> str | None:
        """Reboot the machine in place and wait for it to answer with a new boot id; why it did not, or None."""
        reboot = run_machine_script(
            self.mngr_caller,
            transport,
            address,
            "reboot",
            floor_package_version=self.floor_package_version,
            timeout_seconds=KERNEL_REBOOT_SCHEDULE_TIMEOUT_SECONDS,
        )
        if reboot.status is not KernelScriptStatus.REBOOTING:
            return f"Couldn't restart the machine: {reboot.detail}"
        deadline = time.monotonic() + self.reboot_timeout_seconds
        while time.monotonic() < deadline:
            if self.concurrency_group.shutdown_event.wait(timeout=self.reboot_poll_interval_seconds):
                return "Imbue Studio quit before the machine came back."
            probe = read_kernel_probe(self.mngr_caller, transport, address)
            if probe is not None and probe.boot_id and probe.boot_id != previous_boot_id:
                return None
        return f"The machine did not come back within {self.reboot_timeout_seconds:.0f} seconds of its restart."

    def _probe_until_answered(
        self, transport: KernelProbeTransport, address: str, timeout_seconds: float
    ) -> KernelProbe | None:
        """The first answered probe of a machine that just started, or None when none answers in time."""
        deadline = time.monotonic() + timeout_seconds
        probe = read_kernel_probe(self.mngr_caller, transport, address)
        while probe is None and time.monotonic() < deadline:
            if self.concurrency_group.shutdown_event.wait(timeout=self.reboot_poll_interval_seconds):
                return None
            probe = read_kernel_probe(self.mngr_caller, transport, address)
        return probe

    def _end(self, agent_id: AgentId, *, probe: KernelProbe | None, failure: str) -> None:
        if failure:
            logger.warning("The restart of {} onto the fixed kernel did not land: {}", agent_id, failure)
        else:
            logger.info("The machine of {} is back on the fixed kernel", agent_id)
        self.state_store.end_kernel_fix(agent_id, probe=probe, failure=failure)
        self.on_fix_ended(agent_id)
