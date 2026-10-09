"""The apply window hands an expired window back to the registered recovery dispatcher, past the update veto."""

from pathlib import Path

from flask import Flask

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import _build_workspace_update_machinery
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector
from imbue.minds.desktop_client.workspace_operations import InMemoryWorkspaceOperationRegistry
from imbue.minds.desktop_client.workspace_recovery import UnattendedRecoveryDispatcher
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until

_DRAIN_WAIT_SECONDS = 10.0


def test_the_windows_hand_back_runs_through_the_registered_dispatcher_without_the_update_veto(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """One dispatcher serves the stuck edge and the window's hand-back, so one owed set is drained by one recovery.

    The veto that declines a stuck edge while an apply owns the machine must not
    also decline the window's own hand-back: the window closing is what withdrew
    it. Offline, both paths end in the owed set, which is how the test tells a
    dispatch that went ahead from one that was declined.
    """
    detector, _prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False, is_ssh_up=False)
    assert detector.probe_now().environment_block is EnvironmentBlock.OFFLINE
    workspace_agent = AgentId.generate()
    tracker = SystemInterfaceHealthTracker(stuck_threshold_seconds=0.0)
    dispatcher = UnattendedRecoveryDispatcher(
        tracker=tracker,
        backend_resolver=build_resolver_with_system_services(
            workspace_agent,
            AgentId.generate(),
            provider_name=ProviderInstanceName("imbue_cloud_someone"),
            provider_backend="imbue_cloud",
        ),
        registry=InMemoryWorkspaceOperationRegistry(),
        concurrency_group=root_concurrency_group,
        mngr_binary="mngr",
        mngr_host_dir=tmp_path / "hosts",
        mngr_forward_port=0,
        mngr_forward_preauth_cookie=None,
        connectivity_detector=detector,
        should_decline_dispatch=lambda _agent_id: True,
    )
    machinery = _build_workspace_update_machinery(
        app=Flask(__name__),
        backend_resolver=dispatcher.backend_resolver,
        mngr_caller=RecordingMngrCaller(),
        paths=InstallationPaths(data_dir=tmp_path / "data"),
        minds_config=None,
        system_interface_health_tracker=tracker,
        root_concurrency_group=root_concurrency_group,
        dispatch_restart=dispatcher.dispatch_after_update_window,
        mngr_binary="mngr",
        mngr_host_dir=tmp_path / "hosts",
    )
    assert machinery is not None
    tracker.record_failure(workspace_agent)
    tracker.record_probe_failure(workspace_agent)
    assert tracker.get_health(workspace_agent) is AgentHealth.STUCK

    dispatcher(workspace_agent)
    assert not dispatcher._owed_agent_ids, "the stuck edge is declined while an apply owns the machine"

    machinery.service.apply_window.dispatch_restart(workspace_agent)

    assert poll_until(
        lambda: str(workspace_agent) in dispatcher._owed_agent_ids,
        timeout=_DRAIN_WAIT_SECONDS,
        poll_interval=0.02,
    ), "the hand-back must go ahead past the veto (and be owed, since this device is offline)"
