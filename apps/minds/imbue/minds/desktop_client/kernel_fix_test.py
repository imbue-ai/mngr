"""The restart onto the fixed kernel: what a press is refused for, and the sequence it runs."""

from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.kernel_fix import KernelFixDispatchOutcome
from imbue.minds.desktop_client.kernel_fix import KernelFixRunner
from imbue.minds.desktop_client.kernel_security import KernelMachineAccess
from imbue.minds.desktop_client.kernel_security import KernelProbeTransport
from imbue.minds.desktop_client.kernel_security import KernelRestartMethod
from imbue.minds.desktop_client.testing import FIXED_KERNEL_NAME
from imbue.minds.desktop_client.testing import VULNERABLE_KERNEL_NAME
from imbue.minds.desktop_client.testing import VULNERABLE_KERNEL_PROBE
from imbue.minds.desktop_client.testing import exec_json_envelope
from imbue.minds.desktop_client.testing import host_offline_exec_result
from imbue.minds.desktop_client.testing import machine_script_output
from imbue.minds.desktop_client.testing import make_update_state_store
from imbue.minds.desktop_client.testing import services_agent_listing_line
from imbue.minds.desktop_client.testing import update_run_probe_stdout
from imbue.minds.desktop_client.update_status import KernelFixActivity
from imbue.minds.desktop_client.update_status import KernelSecurityStatus
from imbue.minds.desktop_client.update_status import UpdateAvailability
from imbue.minds.desktop_client.workspace_lifecycle import MindHostAction
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_update_state import UpdateDetection
from imbue.minds.desktop_client.workspace_update_state import WorkspaceUpdateStateStore
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import ScriptedMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until

_FLOOR = "6.12.113-1"
_RUNNING_CHAT = "chat-one\tRUNNING\tagent-" + "1" * 32 + "\t\n"
_IDLE_CHAT = "chat-two\tWAITING\tagent-" + "2" * 32 + "\t\n"
_LIMA = KernelMachineAccess(transport=KernelProbeTransport.AGENT, restart=KernelRestartMethod.HOST_CYCLE)
_VULTR = KernelMachineAccess(transport=KernelProbeTransport.OUTER, restart=KernelRestartMethod.IN_PLACE_REBOOT)
_PAUSED_CLOUD = KernelMachineAccess(transport=KernelProbeTransport.OUTER, restart=KernelRestartMethod.HOST_CYCLE)


def _machine_answer(running: str, boot_id: str, *, installed: str = "", status: str | None = None) -> MngrCallResult:
    """What ``mngr exec --format json`` answers for one run of the machine script."""
    return MngrCallResult(
        returncode=0,
        stdout=exec_json_envelope(
            machine_script_output(running, boot_id=boot_id, installed=installed, status=status, detail="detail")
        ),
    )


def _chats(agent_id: AgentId, chats: str | None) -> MngrCallResult:
    """The chat listing a run probe answers: the services agent plus ``chats``, or a listing that failed."""
    agents = None if chats is None else services_agent_listing_line(agent_id) + chats
    return MngrCallResult(returncode=0, stdout=update_run_probe_stdout(agents=agents))


def _no_chats(agent_id: AgentId) -> MngrCallResult:
    return _chats(agent_id, _IDLE_CHAT)


_INSTALLED = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR, status="installed")
_CAME_BACK_FIXED = _machine_answer(FIXED_KERNEL_NAME, "boot-2", installed=_FLOOR)


class _HostActions:
    """Records the host stops and starts a run asked for, in order."""

    def __init__(self, *, is_stop_successful: bool = True, is_start_successful: bool = True) -> None:
        self.actions: list[MindHostAction] = []
        self.is_stop_successful = is_stop_successful
        self.is_start_successful = is_start_successful

    def stop(self, agent_id: AgentId) -> MindHostActionOutcome:
        self.actions.append(MindHostAction.STOP)
        return MindHostActionOutcome(
            is_successful=self.is_stop_successful, failure_reason=None if self.is_stop_successful else "vm busy"
        )

    def start(self, agent_id: AgentId) -> MindHostActionOutcome:
        self.actions.append(MindHostAction.START)
        return MindHostActionOutcome(
            is_successful=self.is_start_successful, failure_reason=None if self.is_start_successful else "no boot"
        )


def _runner(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    agent_id: AgentId,
    caller: ScriptedMngrCaller,
    *,
    access: KernelMachineAccess | None = _LIMA,
    hosts: _HostActions | None = None,
    ended: list[AgentId] | None = None,
    store: WorkspaceUpdateStateStore | None = None,
) -> tuple[KernelFixRunner, WorkspaceUpdateStateStore]:
    store = store if store is not None else make_update_state_store(tmp_path)
    hosts = hosts if hosts is not None else _HostActions()
    runner = KernelFixRunner(
        state_store=store,
        mngr_caller=caller,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        concurrency_group=concurrency_group,
        kernel_access_for=lambda _agent_id: access,
        stop_workspace=hosts.stop,
        start_workspace=hosts.start,
        on_fix_ended=(ended if ended is not None else []).append,
        floor_package_version=_FLOOR,
        reboot_timeout_seconds=5.0,
        reboot_poll_interval_seconds=0.01,
        verification_timeout_seconds=1.0,
    )
    return runner, store


def _offer_the_fix(store: WorkspaceUpdateStateStore, agent_id: AgentId, transport: KernelProbeTransport) -> None:
    store.record_detection(
        agent_id,
        detection=UpdateDetection(availability=UpdateAvailability.UP_TO_DATE),
        current_version="minds-v0.9.1",
        supported_version="minds-v0.9.1",
        is_readable=True,
    )
    store.record_kernel_probe(agent_id, transport=transport, probe=VULNERABLE_KERNEL_PROBE)


def _wait_until_idle(store: WorkspaceUpdateStateStore, agent_id: AgentId) -> None:
    assert poll_until(lambda: store.get(agent_id).kernel.activity is KernelFixActivity.IDLE, timeout=10.0)


def test_a_machine_with_no_fix_on_offer_is_refused_before_anything_is_asked(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id),))
    runner, _store = _runner(tmp_path, root_concurrency_group, agent_id, caller)

    dispatch = runner.dispatch(agent_id, is_forced=False)

    assert dispatch.outcome is KernelFixDispatchOutcome.NOT_OFFERED
    assert caller.calls == []


def test_a_running_chat_refuses_the_restart_and_is_named_unless_the_press_goes_ahead_anyway(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_chats(agent_id, _RUNNING_CHAT + _IDLE_CHAT), _INSTALLED, _CAME_BACK_FIXED))
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    refused = runner.dispatch(agent_id, is_forced=False)
    forced = runner.dispatch(agent_id, is_forced=True)
    _wait_until_idle(store, agent_id)

    assert refused.outcome is KernelFixDispatchOutcome.CHATS_RUNNING
    assert refused.running_chats == ("chat-one",)
    assert forced.outcome is KernelFixDispatchOutcome.DISPATCHED
    assert store.get(agent_id).kernel.status is KernelSecurityStatus.FIXED


def test_chats_that_cannot_be_listed_refuse_the_restart_rather_than_assume_none_is_running(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_chats(agent_id, None),))
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    dispatch = runner.dispatch(agent_id, is_forced=False)

    assert dispatch.outcome is KernelFixDispatchOutcome.CHATS_UNREADABLE
    assert store.get(agent_id).kernel.activity is KernelFixActivity.IDLE


def test_a_template_update_in_flight_refuses_the_restart(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id),))
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)
    assert store.try_begin_run(agent_id, target_override="")

    dispatch = runner.dispatch(agent_id, is_forced=True)

    assert dispatch.outcome is KernelFixDispatchOutcome.UPDATE_IN_FLIGHT
    assert caller.calls == []


def test_a_lima_machine_is_installed_then_stopped_and_started_and_the_badge_clears_on_a_new_boot(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, _CAME_BACK_FIXED))
    hosts = _HostActions()
    ended: list[AgentId] = []
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, hosts=hosts, ended=ended)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    dispatch = runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert dispatch.outcome is KernelFixDispatchOutcome.DISPATCHED
    assert hosts.actions == [MindHostAction.STOP, MindHostAction.START]
    kernel = store.get(agent_id).kernel
    assert kernel.status is KernelSecurityStatus.FIXED
    assert kernel.is_fix_landed is True
    assert kernel.failure == ""
    assert kernel.is_fix_offered is False
    assert ended == [agent_id]
    # The chat listing, the install, and the verification: no reboot script for a VM its own start boots.
    assert [argv[0] for argv in caller.calls] == ["exec", "exec", "exec"]
    assert all("--outer" not in argv for argv in caller.calls)


def test_a_cloud_machine_is_rebooted_in_place_and_its_container_started_once_it_answers_with_a_new_boot(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    rebooting = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR, status="rebooting")
    still_down = MngrCallResult(returncode=255, stderr="ssh: connect to host refused")
    not_yet = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR)
    caller = ScriptedMngrCaller(
        results=(_no_chats(agent_id), _INSTALLED, rebooting, still_down, not_yet, _CAME_BACK_FIXED, _CAME_BACK_FIXED)
    )
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, access=_VULTR, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.OUTER)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == [MindHostAction.STOP, MindHostAction.START]
    assert store.get(agent_id).kernel.status is KernelSecurityStatus.FIXED
    assert store.get(agent_id).kernel.failure == ""
    # Everything after the chat listing goes through the outer host, the reboot included.
    assert all("--outer" in argv for argv in caller.calls[1:])
    assert any("bash -s -- reboot" in argv[-3] for argv in caller.calls)


def test_a_cloud_instance_its_host_stop_pauses_is_booted_by_the_start_and_never_rebooted_in_place(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, _CAME_BACK_FIXED))
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, access=_PAUSED_CLOUD, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.OUTER)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == [MindHostAction.STOP, MindHostAction.START]
    kernel = store.get(agent_id).kernel
    assert kernel.status is KernelSecurityStatus.FIXED
    assert kernel.is_fix_landed is True
    # The install and the verification go through the outer host; a paused instance has nothing to reboot.
    assert all("--outer" in argv for argv in caller.calls[1:])
    assert not any("bash -s -- reboot" in argv[-3] for argv in caller.calls)


def test_a_cloud_machine_that_never_comes_back_is_reported_rather_than_called_done(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    rebooting = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR, status="rebooting")
    same_boot = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR)
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, rebooting, same_boot))
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, access=_VULTR, hosts=hosts)
    runner.reboot_timeout_seconds = 0.05
    _offer_the_fix(store, agent_id, KernelProbeTransport.OUTER)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    kernel = store.get(agent_id).kernel
    assert kernel.status is KernelSecurityStatus.VULNERABLE
    assert "did not come back" in kernel.failure
    assert kernel.is_fix_landed is False
    # The container stopped for the reboot is brought back rather than left down.
    assert hosts.actions == [MindHostAction.STOP, MindHostAction.START]


def test_a_container_that_cannot_be_started_after_a_failed_reboot_is_named_beside_the_reboot(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    could_not_schedule = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", installed=_FLOOR, status="failed")
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, could_not_schedule))
    hosts = _HostActions(is_start_successful=False)
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, access=_VULTR, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.OUTER)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == [MindHostAction.STOP, MindHostAction.START]
    failure = store.get(agent_id).kernel.failure
    assert failure.startswith("Couldn't restart the machine")
    assert "could not be started again either: no boot" in failure


def test_an_install_that_fails_ends_the_restart_before_the_machine_is_touched(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    failed = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-1", status="failed")
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), failed))
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == []
    kernel = store.get(agent_id).kernel
    assert kernel.failure.startswith("Couldn't install the kernel package")
    assert kernel.is_fix_offered is True


def test_a_machine_that_comes_back_on_the_old_kernel_keeps_its_badge_and_says_so(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    came_back_old = _machine_answer(VULNERABLE_KERNEL_NAME, "boot-2", installed=_FLOOR)
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, came_back_old))
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    kernel = store.get(agent_id).kernel
    assert kernel.status is KernelSecurityStatus.VULNERABLE
    assert VULNERABLE_KERNEL_NAME in kernel.failure
    assert kernel.is_fix_offered is True


def test_a_second_press_while_the_restart_runs_is_refused(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), _INSTALLED, _CAME_BACK_FIXED))
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)
    assert store.try_begin_kernel_fix(agent_id)

    dispatch = runner.dispatch(agent_id, is_forced=True)

    assert dispatch.outcome is KernelFixDispatchOutcome.ALREADY_RUNNING
    store.end_kernel_fix(agent_id, probe=None, failure="")


def test_a_machine_the_press_finds_stopped_is_started_for_the_install_and_restarted_as_usual(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The restart brings the machine up anyway, so a stopped one is started first rather than refused."""
    caller = ScriptedMngrCaller(
        results=(_no_chats(agent_id), host_offline_exec_result(), _INSTALLED, _CAME_BACK_FIXED)
    )
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == [MindHostAction.START, MindHostAction.STOP, MindHostAction.START]
    assert store.get(agent_id).kernel.status is KernelSecurityStatus.FIXED
    assert store.get(agent_id).kernel.failure == ""


def test_a_stopped_machine_that_will_not_start_ends_the_restart_with_the_start_failure(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), host_offline_exec_result()))
    hosts = _HostActions(is_start_successful=False)
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == [MindHostAction.START]
    assert store.get(agent_id).kernel.failure == "Couldn't start the machine: no boot"
    assert store.get(agent_id).kernel.is_fix_offered is True


def test_an_install_that_times_out_ends_the_restart_without_starting_the_machine(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup, agent_id: AgentId
) -> None:
    """The install may still be running on the machine, so it is neither started nor installed again."""
    timed_out = MngrCallResult(returncode=-1, stderr="mngr exec timed out after 900s", is_timed_out=True)
    caller = ScriptedMngrCaller(results=(_no_chats(agent_id), timed_out))
    hosts = _HostActions()
    runner, store = _runner(tmp_path, root_concurrency_group, agent_id, caller, hosts=hosts)
    _offer_the_fix(store, agent_id, KernelProbeTransport.AGENT)

    runner.dispatch(agent_id, is_forced=False)
    _wait_until_idle(store, agent_id)

    assert hosts.actions == []
    assert len(caller.calls) == 2
    assert (
        store.get(agent_id).kernel.failure
        == "Couldn't install the kernel package: could not reach the machine: timed out"
    )
