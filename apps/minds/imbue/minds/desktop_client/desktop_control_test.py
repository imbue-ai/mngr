"""Tests for the install-scoped desktop control helpers."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.desktop_control import stop_workspace_hosts
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import SUPPRESSION_WAIT_SECONDS
from imbue.minds.desktop_client.testing import SuppressionAnnouncingTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import write_blocking_stub_mngr
from imbue.minds.desktop_client.testing import write_stub_mngr
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName

_HOST = HostId("host-" + "0" * 31 + "1")


def _resolver_with_one_running_machine() -> tuple[AgentId, MngrCliBackendResolver]:
    """A resolver carrying one RUNNING docker machine and the system-services agent beside it.

    The labels and the RUNNING host state are what ``running_workspace_entries``
    reads, so the bulk stop's "still running" answer -- which it reconciles the
    intentional-stop marks against -- is a real liveness reading.
    """
    workspace_agent = AgentId.generate()
    resolver = build_resolver_with_system_services(
        workspace_agent,
        AgentId.generate(),
        host_id=_HOST,
        host_state=HostState.RUNNING,
        workspace_agent_name=AgentName("machine"),
        workspace_certified_data={"labels": {"workspace": "my-workspace", "is_primary": "true"}},
    )
    seed_provider_snapshots(
        resolver,
        providers=(
            make_discovered_provider(
                ProviderInstanceName("docker"),
                ProviderInstanceConfig(backend=ProviderBackendName("docker"), is_enabled=True),
            ),
        ),
        error_by_provider_name={},
        last_snapshot_at=datetime.now(timezone.utc),
    )
    return workspace_agent, resolver


def _stop_hosts(
    tracker: SystemInterfaceHealthTracker, tmp_path: Path, mngr_body: str
) -> tuple[AgentId, list[dict[str, str]]]:
    """Run the quit-time bulk stop against a stub mngr, returning the machine and what is still up."""
    workspace_agent, resolver = _resolver_with_one_running_machine()
    with ConcurrencyGroup(name="test-desktop-control") as cg:
        still_running = stop_workspace_hosts(
            [str(workspace_agent)],
            resolver,
            write_stub_mngr(tmp_path, "stub_mngr", mngr_body),
            tmp_path,
            cg,
            health_tracker=tracker,
        )
    return workspace_agent, still_running


def test_the_quit_time_bulk_stop_blocks_unattended_recovery(tmp_path: Path) -> None:
    """The quit prompt's "shut down all" marks each machine, so the app cannot start it back up.

    The quit sequence does not end when the stop returns: a partial stop offers
    Cancel quit, which resumes the app with these machines down and their
    windows still open. Their dying interfaces enroll probe suspects, the probe
    loop drives them STUCK, and without the mark the unattended dispatch starts
    them again under the user.
    """
    tracker = SystemInterfaceHealthTracker()

    workspace_agent, still_running = _stop_hosts(tracker, tmp_path, "exit 0")

    assert still_running == []
    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is True


def test_a_quit_time_bulk_stop_that_failed_leaves_the_machine_healing_itself(tmp_path: Path) -> None:
    """A machine reported as still running must not be left excluded from self-healing.

    The mark goes on before the command, so the machines the stop did not take
    down owe the revert -- they are exactly the ones the quit flow offers Retry
    for, and they go on serving the user in the meantime.
    """
    tracker = SystemInterfaceHealthTracker()

    workspace_agent, still_running = _stop_hosts(
        tracker, tmp_path, 'echo "ERROR: could not stop the host" >&2\nexit 1'
    )

    assert [entry["id"] for entry in still_running] == [str(workspace_agent)]
    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is False


def test_a_probe_that_caught_a_machine_still_up_does_not_unmark_the_quit_stop(tmp_path: Path) -> None:
    """A 200 taken mid-stop must not hand a quitting machine back to unattended recovery.

    The bulk stop marks before its ``mngr`` runs and the interfaces answer for
    the first seconds after, so an ordinary mark would be cleared by the probe
    loop's own success -- and the closing mark, which lands only when the
    command returns, would hide it. Cancel quit then resumes the app with a
    machine that is down and no longer marked.
    """
    tracker = SuppressionAnnouncingTracker()
    workspace_agent, resolver = _resolver_with_one_running_machine()
    release_path = tmp_path / "release-the-stop"
    mngr_binary = write_blocking_stub_mngr(tmp_path, "stub_mngr", release_path)

    with ConcurrencyGroup(name="test-desktop-control") as cg:
        stop_thread = cg.start_new_thread(
            target=stop_workspace_hosts,
            args=([str(workspace_agent)], resolver, mngr_binary, tmp_path, cg),
            kwargs={"health_tracker": tracker},
            name="test-blocking-bulk-stop",
        )
        assert tracker.wait_for_suppression(SUPPRESSION_WAIT_SECONDS), "the bulk stop must mark before it runs"
        tracker.record_probe_success(workspace_agent)
        # Read while the stop still blocks: this IS the window under test, and
        # the stop's own closing mark would paper over a mark dropped here.
        is_suppressed_mid_stop = tracker.is_unattended_recovery_suppressed(workspace_agent)
        release_path.write_text("go\n")
        stop_thread.join(timeout=SUPPRESSION_WAIT_SECONDS)
        assert not stop_thread.is_alive(), "the released stop must return before the group closes"

    assert is_suppressed_mid_stop is True
