"""Tests for the shared workspace host lifecycle helpers."""

from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import SUPPRESSION_WAIT_SECONDS
from imbue.minds.desktop_client.testing import SuppressionAnnouncingTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import write_blocking_stub_mngr
from imbue.minds.desktop_client.testing import write_stub_mngr
from imbue.minds.desktop_client.workspace_lifecycle import MindHostAction
from imbue.minds.desktop_client.workspace_lifecycle import MindHostActionOutcome
from imbue.minds.desktop_client.workspace_lifecycle import _lead_with_error_lines
from imbue.minds.desktop_client.workspace_lifecycle import perform_mind_host_action
from imbue.mngr.primitives import AgentId


def test_lead_with_error_lines_puts_the_verdict_ahead_of_the_warnings() -> None:
    """The ERROR line names the host asked about; the warnings name other ones.

    Leading with all of stderr made a start failure read as an unreachable box --
    the warnings are about orphaned key dirs for long-destroyed workspaces, while
    the real reason (mngr looked for the agent under the wrong host_dir) was the
    last line.
    """
    stderr = (
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-a15c1302: "
        "Host not found: host-a15c1302\n"
        "ERROR: Agent agent-b790 not found on host host-67ad\n"
    )

    reordered = _lead_with_error_lines(stderr)

    assert reordered.splitlines()[0] == "ERROR: Agent agent-b790 not found on host host-67ad"


def test_lead_with_error_lines_keeps_every_warning() -> None:
    """Reordering, not filtering: the warnings are real diagnostics.

    The caller is an agent on another host with no access to this one's logs, so
    dropping them costs it the only copy it will ever see.
    """
    stderr = (
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-a15c1302: "
        "Host not found: host-a15c1302\n"
        "WARNING: imbue_cloud[acct] outer SSH unreachable for host host-0b17800a: "
        "Host not found: host-0b17800a\n"
        "ERROR: Agent agent-b790 not found on host host-67ad\n"
    )

    reordered = _lead_with_error_lines(stderr)

    assert "Host not found: host-a15c1302" in reordered
    assert "Host not found: host-0b17800a" in reordered
    # Nothing is invented or lost -- the same lines, reordered.
    assert sorted(reordered.splitlines()) == sorted(line.rstrip() for line in stderr.splitlines() if line.strip())


def test_lead_with_error_lines_keeps_every_error_line() -> None:
    """A run that failed several ways must report all of them, in order."""
    stderr = "ERROR: first thing broke\nWARNING: noise\nERROR: second thing broke\n"

    assert _lead_with_error_lines(stderr).splitlines() == [
        "ERROR: first thing broke",
        "ERROR: second thing broke",
        "WARNING: noise",
    ]


def test_lead_with_error_lines_passes_through_output_with_no_verdict() -> None:
    """Not every failure announces itself with an ERROR: prefix."""
    assert _lead_with_error_lines("  Traceback (most recent call last)  ") == "Traceback (most recent call last)"


def _fake_mngr(tmp_path: Path) -> str:
    """A stub ``mngr`` succeeding for any subcommand."""
    return write_stub_mngr(tmp_path, "fake_mngr", "exit 0")


def _failing_mngr(tmp_path: Path) -> str:
    """A stub ``mngr`` whose subcommand fails the way a refused stop does."""
    return write_stub_mngr(tmp_path, "failing_mngr", 'echo "ERROR: could not stop the host" >&2\nexit 1')


def _resolver_for_one_machine() -> tuple[AgentId, MngrCliBackendResolver]:
    """A resolver that knows one workspace and the system-services agent stop/start targets."""
    workspace_agent = AgentId.generate()
    return workspace_agent, build_resolver_with_system_services(workspace_agent, AgentId.generate())


def _perform(
    action: MindHostAction,
    tracker: SystemInterfaceHealthTracker,
    tmp_path: Path,
    mngr_binary: str | None = None,
) -> tuple[AgentId, MindHostActionOutcome]:
    """Run one host action against stub mngr, returning the workspace and outcome."""
    workspace_agent, resolver = _resolver_for_one_machine()
    with ConcurrencyGroup(name="test-lifecycle") as cg:
        outcome = perform_mind_host_action(
            workspace_agent,
            action,
            resolver,
            mngr_binary if mngr_binary is not None else _fake_mngr(tmp_path),
            tmp_path,
            cg,
            ui_publisher=None,
            health_tracker=tracker,
        )
    return workspace_agent, outcome


def test_stopping_a_machine_from_the_app_blocks_unattended_recovery(tmp_path: Path) -> None:
    """The in-app stop marks the tracker, which is what stops the app undoing the stop.

    A stopped host's system interface is unreachable, so the probe loop drives
    it STUCK exactly as a wedge would and the unattended dispatch would start it
    straight back up -- billing the user for a machine they just turned off.
    """
    tracker = SystemInterfaceHealthTracker()

    workspace_agent, _ = _perform(MindHostAction.STOP, tracker, tmp_path)

    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is True


def test_starting_a_machine_from_the_app_restores_unattended_recovery(tmp_path: Path) -> None:
    """Starting it again is the user saying they want it running, so wedges self-heal again."""
    tracker = SystemInterfaceHealthTracker()

    workspace_agent, _ = _perform(MindHostAction.START, tracker, tmp_path)

    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is False


def test_a_stop_blocks_unattended_recovery_before_mngr_returns(tmp_path: Path) -> None:
    """The mark has to be up while the stop runs, not once it finishes.

    The interface dies within seconds of the stop starting, and the probe loop
    needs only ``stuck_threshold_seconds`` of that to hand the machine to the
    unattended dispatch -- long before a stop that takes tens of seconds (a
    cloud host, minutes) returns. A mark applied at the end loses that race and
    the app starts the machine straight back up.
    """
    tracker = SuppressionAnnouncingTracker()
    workspace_agent, resolver = _resolver_for_one_machine()
    release_path = tmp_path / "release-the-stop"

    with ConcurrencyGroup(name="test-lifecycle") as cg:
        stop_thread = cg.start_new_thread(
            target=perform_mind_host_action,
            args=(
                workspace_agent,
                MindHostAction.STOP,
                resolver,
                write_blocking_stub_mngr(tmp_path, "blocking_mngr", release_path),
                tmp_path,
                cg,
            ),
            kwargs={"ui_publisher": None, "health_tracker": tracker},
            name="test-blocking-stop",
        )
        # The stub cannot return until released below, so a mark observed here
        # is one that landed while the stop was still running.
        is_suppressed_mid_stop = tracker.wait_for_suppression(SUPPRESSION_WAIT_SECONDS)
        release_path.write_text("go\n")
        # Joined inside the group: the stop runs its mngr command *through* this
        # group, and leaving the block flips it out of ACTIVE before it joins
        # the strands -- so a stop still in flight would wake to a group that
        # refuses to run processes and fail the strand.
        stop_thread.join(timeout=SUPPRESSION_WAIT_SECONDS)
        # join() reports nothing about whether it got there, and a timeout would
        # leave the block with the stop still in flight.
        assert not stop_thread.is_alive(), "the released stop must return before the group closes"

    assert is_suppressed_mid_stop is True
    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is True


def test_a_probe_that_caught_the_machine_still_up_does_not_unmark_the_stop(tmp_path: Path) -> None:
    """A 200 observed mid-stop must not hand the machine back to unattended recovery.

    The mark goes on before ``mngr stop`` runs, so it is live while the
    interface is still answering -- and a machine being stopped is often a
    probe target already (suspect-enrolled, STUCK, or RECOVERY_FAILED). Were a
    probe to clear the mark there, the dying interface would re-enroll the agent
    through its window's failure envelopes, it would reach STUCK, and the
    dispatch would start the machine back up under a stop that is still running.
    """
    tracker = SuppressionAnnouncingTracker()
    workspace_agent, resolver = _resolver_for_one_machine()
    release_path = tmp_path / "release-the-stop"

    with ConcurrencyGroup(name="test-lifecycle") as cg:
        stop_thread = cg.start_new_thread(
            target=perform_mind_host_action,
            args=(
                workspace_agent,
                MindHostAction.STOP,
                resolver,
                write_blocking_stub_mngr(tmp_path, "blocking_mngr", release_path),
                tmp_path,
                cg,
            ),
            kwargs={"ui_publisher": None, "health_tracker": tracker},
            name="test-blocking-stop",
        )
        assert tracker.wait_for_suppression(SUPPRESSION_WAIT_SECONDS), "the stop must mark before it runs"
        # What the probe loop does when it polls the machine in the seconds
        # between the stop being asked for and the interface going down.
        tracker.record_probe_success(workspace_agent)
        # Asserted here, not only after the stop returns: the window this covers
        # IS the one in which the stop is still running, and the stop's own
        # closing mark would hide a mark that had been dropped in between.
        is_suppressed_mid_stop = tracker.is_unattended_recovery_suppressed(workspace_agent)
        release_path.write_text("go\n")
        stop_thread.join(timeout=SUPPRESSION_WAIT_SECONDS)
        assert not stop_thread.is_alive(), "the released stop must return before the group closes"

    assert is_suppressed_mid_stop is True
    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is True


def test_a_stop_that_failed_leaves_the_machine_healing_itself(tmp_path: Path) -> None:
    """A stop that never happened must not exclude the machine from recovery for good.

    The mark goes on before the command, so the failure path owes the revert --
    otherwise a refused stop would silently disarm self-healing for the rest of
    the process's life.
    """
    tracker = SystemInterfaceHealthTracker()

    workspace_agent, outcome = _perform(MindHostAction.STOP, tracker, tmp_path, mngr_binary=_failing_mngr(tmp_path))

    assert outcome.is_successful is False
    assert tracker.is_unattended_recovery_suppressed(workspace_agent) is False
