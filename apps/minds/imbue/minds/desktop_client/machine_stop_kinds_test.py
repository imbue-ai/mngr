from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.imbue_cloud_cli import MachineSizeCliInfo
from imbue.minds.desktop_client.machine_stop_kinds import MachineStopKindTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

_ACCOUNT_USER_ID = "u" * 36
_ACCOUNT_EMAIL = "alice@imbue.com"
_CLOUD_PROVIDER = ProviderInstanceName("imbue_cloud_alice-imbue-com")


def _machine(host_id: HostId, status: str, stop_kind: str | None) -> MachineSizeCliInfo:
    return MachineSizeCliInfo(
        host_db_id="11111111-2222-3333-4444-555555555555",
        host_id=str(host_id),
        host_name="ws",
        status=status,
        stop_kind=stop_kind,
    )


def _build(
    tmp_path: Path, host_state: HostState | None
) -> tuple[MachineStopKindTracker, FakeImbueCloudCli, AgentId, HostId]:
    """A tracker over one signed-in account's cloud workspace in ``host_state``; each test fills in the fake CLI's listing."""
    workspace_agent = AgentId.generate()
    services_agent = AgentId.generate()
    host_id = HostId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent,
        services_agent,
        host_id=host_id,
        host_state=host_state,
        # The primary label is what makes the agent an active workspace to the resolver.
        workspace_certified_data={"labels": {"is_primary": "true"}},
        provider_name=_CLOUD_PROVIDER,
        provider_backend="imbue_cloud",
    )
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(_ACCOUNT_USER_ID, _ACCOUNT_EMAIL)
    session_store = make_session_store_for_test(tmp_path, cli=cli)
    session_store.associate_workspace(_ACCOUNT_USER_ID, str(workspace_agent), resolver)
    tracker = MachineStopKindTracker(backend_resolver=resolver, session_store=session_store, imbue_cloud_cli=cli)
    return tracker, cli, workspace_agent, host_id


@pytest.mark.witnesses(
    "machine-lifecycle.held-machine-maintenance",
    partial="witnesses the held kind reaching the list's data; the badge, the withheld Start and the band are witnessed by the SPA's own suite",
)
@pytest.mark.witnesses(
    "machine-lifecycle.owner-startable-idle",
    partial="witnesses the idle kind reaching the list's data; the plain badge, the offered Start and its press are witnessed by the SPA's own suite",
)
@pytest.mark.parametrize("stop_kind", ["maintenance", "idle"])
def test_refresh_reads_the_kind_of_a_stopped_cloud_machine_and_tells_the_publisher(
    tmp_path: Path, stop_kind: str
) -> None:
    changes: list[int] = []
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.STOPPED)
    cli.machines = [_machine(host_id, "stopped", stop_kind)]
    tracker.on_change = lambda: changes.append(1)

    assert tracker.refresh() is True
    assert tracker.stop_kind_by_host_id() == {str(host_id): stop_kind}
    assert changes == [1]
    # A second identical pass changes nothing and tells nobody.
    assert tracker.refresh() is False
    assert changes == [1]
    assert cli.machine_list_call_count == 2


def test_refresh_makes_no_listing_while_every_cloud_machine_is_running(tmp_path: Path) -> None:
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.RUNNING)
    cli.machines = [_machine(host_id, "running", None)]

    assert tracker.refresh() is False
    assert tracker.stop_kind_by_host_id() == {}
    assert cli.machine_list_call_count == 0


def test_a_failed_listing_keeps_the_last_read_kinds(tmp_path: Path) -> None:
    # A hold must not flicker off (offering Start) because one read failed.
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.STOPPED)
    cli.machines = [_machine(host_id, "stopped", "maintenance")]
    assert tracker.refresh() is True

    cli.is_machine_listing_failing = True
    assert tracker.refresh() is False
    assert tracker.stop_kind_by_host_id() == {str(host_id): "maintenance"}


def test_a_failed_account_listing_keeps_the_last_read_kinds(tmp_path: Path) -> None:
    # The accounts come from `auth list`, which fails under the same outage as
    # the machine listing; an empty account list is then no evidence that the
    # hold is gone.
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.STOPPED)
    cli.machines = [_machine(host_id, "stopped", "maintenance")]
    assert tracker.refresh() is True

    assert tracker.session_store is not None
    tracker.session_store.invalidate_identity_cache()
    cli.is_auth_list_failing = True
    assert tracker.refresh() is False
    assert tracker.stop_kind_by_host_id() == {str(host_id): "maintenance"}
    assert cli.machine_list_call_count == 1


def test_a_wake_lists_only_when_a_cloud_machine_changed_lifecycle_state(tmp_path: Path) -> None:
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.RUNNING)
    cli.machines = [_machine(host_id, "stopping", "maintenance")]
    assert tracker.refresh(is_only_on_state_change=True) is False
    assert cli.machine_list_call_count == 0

    # Discovery now reports the machine on its way down: the wake lists at once.
    tracker.backend_resolver.set_host_state_override(host_id, HostState.STOPPING)
    assert tracker.refresh(is_only_on_state_change=True) is True
    assert tracker.stop_kind_by_host_id() == {str(host_id): "maintenance"}
    assert cli.machine_list_call_count == 1
    # Another resolver event with nothing moved costs no listing; the timed pass still lists.
    assert tracker.refresh(is_only_on_state_change=True) is False
    assert cli.machine_list_call_count == 1
    assert tracker.refresh() is False
    assert cli.machine_list_call_count == 2


def test_a_wake_retries_the_edge_listing_that_failed(tmp_path: Path) -> None:
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.RUNNING)
    cli.machines = [_machine(host_id, "stopping", "maintenance")]
    assert tracker.refresh(is_only_on_state_change=True) is False
    tracker.backend_resolver.set_host_state_override(host_id, HostState.STOPPING)

    cli.is_machine_listing_failing = True
    assert tracker.refresh(is_only_on_state_change=True) is False
    # The edge is not marked as read: the next wake lists again instead of
    # leaving the kind to the timed pass.
    cli.is_machine_listing_failing = False
    assert tracker.refresh(is_only_on_state_change=True) is True
    assert tracker.stop_kind_by_host_id() == {str(host_id): "maintenance"}
    assert cli.machine_list_call_count == 2


@pytest.mark.witnesses(
    "machine-lifecycle.unknown-stop-kind-not-actionable",
    partial="witnesses the kind reading as unknown; the withheld Start is witnessed by the SPA's own suite and the refused start by the plugin's",
)
def test_refresh_reads_an_unrecognized_kind_as_unknown(tmp_path: Path) -> None:
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.STOPPED)
    cli.machines = [_machine(host_id, "stopped", "quarantine")]

    tracker.refresh()

    assert tracker.stop_kind_by_host_id() == {str(host_id): "unknown"}


def test_read_lifecycle_asks_the_connector_live_for_the_workspace(tmp_path: Path) -> None:
    tracker, cli, agent, host_id = _build(tmp_path, HostState.RUNNING)
    cli.machines = [_machine(host_id, "stopping", "maintenance")]

    assert tracker.read_lifecycle(agent) is WorkspaceStatus.STOPPING
    assert cli.machine_show_call_count == 1
    cli.machines = []
    assert tracker.read_lifecycle(agent) is None


def test_read_lifecycle_answers_none_without_a_cli(tmp_path: Path) -> None:
    tracker, _cli, agent, _host_id = _build(tmp_path, HostState.STOPPED)
    silent = MachineStopKindTracker(
        backend_resolver=tracker.backend_resolver, session_store=tracker.session_store, imbue_cloud_cli=None
    )
    assert silent.read_lifecycle(agent) is None
    assert silent.refresh() is False


def test_the_loop_still_lists_on_its_cadence_while_wakes_keep_arriving(tmp_path: Path) -> None:
    # Discovery fires the wake on every snapshot, at the poll's own cadence; a
    # wake must not push the full pass back, or a kind changed server-side under
    # an unchanged state (the operator's hand-back) would never be re-read.
    tracker, cli, _agent, host_id = _build(tmp_path, HostState.STOPPED)
    cli.machines = [_machine(host_id, "stopped", "maintenance")]
    looping = MachineStopKindTracker(
        backend_resolver=tracker.backend_resolver,
        session_store=tracker.session_store,
        imbue_cloud_cli=cli,
        poll_interval_seconds=0.05,
    )
    with ConcurrencyGroup(name="test-machine-stop-kinds-loop") as concurrency_group:
        looping.start(concurrency_group)
        try:

            def _wake_and_check_for_three_listings() -> bool:
                looping.request_refresh()
                return cli.machine_list_call_count >= 3

            is_listed_three_times = poll_until(_wake_and_check_for_three_listings, timeout=2.0, poll_interval=0.005)
        finally:
            looping.stop()
    assert is_listed_three_times
    assert looping.stop_kind_by_host_id() == {str(host_id): "maintenance"}
