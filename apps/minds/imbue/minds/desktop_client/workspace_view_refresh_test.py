"""Unit coverage for the gate that decides *when* a recovered machine's view is refreshed.

The refresh itself is one published frame and is covered by the publisher's own
tests. What is worth pinning here is the timing rule, because the bug it exists
for is invisible to every other check: the frame publishes, the window obeys it,
the reload commits -- and the page is blank anyway, because the reload was
issued into a network that was still coming back.
"""

import queue
import time
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.environment_signals import ConnectivityReading
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.testing import STUB_CONNECTIVITY_HOSTS
from imbue.minds.desktop_client.testing import bring_stub_network_back
from imbue.minds.desktop_client.testing import bring_stub_network_up
from imbue.minds.desktop_client.testing import build_resolver_with_provider_backend
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector
from imbue.minds.desktop_client.testing import build_ui_state_publisher_for_test
from imbue.minds.desktop_client.testing import drain_ui_channel_frames
from imbue.minds.desktop_client.workspace_view_refresh import WorkspaceViewRefresher
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until

# Long enough that "nothing has published yet" cannot pass by being quick, and
# that a settle worker still waiting is unmistakably still waiting.
_SETTLE_SECONDS: Final[float] = 0.5

# Ceiling on "the held refresh has been published": the wait ends the instant
# the frame lands (measured at 0.5-2.6s), so this only bounds a failing run.
# Kept inside the suite's own ``--timeout=10`` per-test budget -- and inside
# what a test that already spent a settle window has left of it -- so a
# regression fails on the assertion that says what went wrong rather than on
# pytest's opaque timeout.
_PUBLISH_WAIT_SECONDS: Final[float] = 5.0


def _refreshed_agent_ids(client_queue: "queue.Queue[str | None]") -> list[str]:
    return [
        frame["agent_id"] for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "workspace_refresh"
    ]


def _wait_for_refreshes(client_queue: "queue.Queue[str | None]") -> list[str]:
    """Poll until a refresh frame lands, and answer with the ids seen.

    The drain is destructive, so what a pass reads has to be accumulated rather
    than re-read once the poll returns.
    """
    refreshed: list[str] = []

    def _has_refreshed() -> bool:
        refreshed.extend(_refreshed_agent_ids(client_queue))
        return bool(refreshed)

    assert poll_until(_has_refreshed, timeout=_PUBLISH_WAIT_SECONDS), "the held view refresh was never published"
    return refreshed


def test_a_refresh_on_a_device_with_no_network_trouble_publishes_immediately() -> None:
    """The ordinary case -- a machine that was genuinely down came back -- is not delayed."""
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-online") as concurrency_group:
        detector, _prober = build_stub_connectivity_detector(concurrency_group)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )

        refresher(agent_id)

        assert _refreshed_agent_ids(client_queue) == [str(agent_id)]


def test_a_refresh_for_a_machine_on_this_device_publishes_while_the_device_is_offline() -> None:
    """The hold is for machines reached over the network, and only those.

    A docker container answers over loopback, so the dead wifi neither caused
    its outage nor threatens the reload -- and holding it would strand it: the
    release is the network coming back, which on a device that stays offline
    never arrives.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-on-device") as concurrency_group:
        detector, _prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            backend_resolver=build_resolver_with_provider_backend(agent_id, provider_name="docker", backend="docker"),
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )

        refresher(agent_id)

        assert _refreshed_agent_ids(client_queue) == [str(agent_id)]


def test_a_refresh_with_no_detector_to_consult_publishes_immediately(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """Without the environment signals wired up, the gate is not in the path at all."""
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()

    WorkspaceViewRefresher(publisher=publisher, concurrency_group=root_concurrency_group)(agent_id)

    assert _refreshed_agent_ids(client_queue) == [str(agent_id)]


def test_a_refresh_raised_while_the_device_is_offline_waits_for_the_network_to_settle() -> None:
    """The incident's shape: the machine's recovery edge arrives before connectivity's does.

    The reload the frame asks for cannot survive at that instant, so the frame
    is held and published a settle later.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-offline") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)

        # The machine answers again -- which it can only do because the network
        # is already back, a moment before the detector reads it that way.
        refresher(agent_id)
        assert _refreshed_agent_ids(client_queue) == []

        started_at = time.monotonic()
        bring_stub_network_back(detector, prober)
        assert _refreshed_agent_ids(client_queue) == []

        assert _wait_for_refreshes(client_queue) == [str(agent_id)]
        assert time.monotonic() - started_at >= _SETTLE_SECONDS


def test_a_refresh_raised_just_after_connectivity_returned_waits_out_the_rest_of_the_settle() -> None:
    """The other ordering, which a "is the device blocked *right now*" check would miss.

    The two edges are independent: connectivity can read good a moment before the
    machine's own probe succeeds, and the network is no more settled for it.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-inside-settle") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)

        started_at = time.monotonic()
        bring_stub_network_back(detector, prober)
        # Nothing was held when the settle began; this machine's edge lands
        # inside it, on a reading that already says the device is fine.
        refresher(agent_id)
        assert _refreshed_agent_ids(client_queue) == []

        assert _wait_for_refreshes(client_queue) == [str(agent_id)]
        assert time.monotonic() - started_at >= _SETTLE_SECONDS


def test_a_network_that_drops_again_inside_the_settle_keeps_the_refresh_held() -> None:
    """The settle is a claim about the network *now*, not about the recovery that armed it.

    Publishing on the strength of a recovery the interface has already undone is
    the blank frame this module exists to prevent, so the worker declines and
    leaves the release to the recovery that comes next.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-reflap") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)

        refresher(agent_id)
        bring_stub_network_back(detector, prober)
        # Back down again while the settle worker is still waiting.
        prober.reachable_hosts = set()
        prober.ssh_endpoints = set()
        detector.probe_now()

        # Watched for four settles, so the worker armed by the recovery that no
        # longer holds has certainly run its course and declined.
        assert not poll_until(
            lambda: bool(_refreshed_agent_ids(client_queue)), timeout=_SETTLE_SECONDS * 4, poll_interval=0.02
        )

        # The recovery that does hold is what publishes it.
        bring_stub_network_back(detector, prober)
        assert _wait_for_refreshes(client_queue) == [str(agent_id)]


class _RecoveringMidCallDetector(ConnectivityDetector):
    """Reads OFFLINE once, then NONE: the network comes back *during* one gate call.

    The detector fires its recovery on the bad -> good edge alone, so a settle
    that drained before this refresh joined the held set is the only one coming.
    """

    def get_reading(self) -> ConnectivityReading:
        reading = super().get_reading()
        if reading.environment_block is not EnvironmentBlock.NONE:
            self.probe_now()
        return reading


def test_a_refresh_that_joins_the_held_set_after_its_settle_drained_is_not_stranded(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    _, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)
    detector = _RecoveringMidCallDetector(
        prober=prober,
        probe_hosts=STUB_CONNECTIVITY_HOSTS,
        poll_interval_seconds=0.02,
        workspace_ssh_endpoints_fn=lambda: (),
        concurrency_group=root_concurrency_group,
    )
    detector.probe_now()
    bring_stub_network_up(prober)
    # No settle to wait out: the point is that the drain has already been and
    # gone by the time the refresh is recorded, not how long it took.
    refresher = WorkspaceViewRefresher(
        publisher=publisher,
        connectivity_detector=detector,
        concurrency_group=root_concurrency_group,
        settle_seconds=0.0,
    )
    detector.add_on_recovery_callback(refresher.on_connectivity_recovered)

    refresher(agent_id)

    assert _refreshed_agent_ids(client_queue) == [str(agent_id)]


def test_a_recovery_that_started_no_settle_worker_opens_no_window_to_be_held_in(
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A settle window nothing will end must not hold anything.

    The window's only other release is another bad -> good edge, and the
    detector fires that once, on the transition this recovery *was*. So a
    machine held in a window with no worker keeps its dead page for the life of
    the process. Reached here through a group that has already exited, which is
    what refuses the spawn during a quit.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    detector, prober = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)
    detector.probe_now()
    with ConcurrencyGroup(name="test-view-refresh-exited") as exited_group:
        pass
    refresher = WorkspaceViewRefresher(
        publisher=publisher,
        connectivity_detector=detector,
        concurrency_group=exited_group,
        settle_seconds=_SETTLE_SECONDS,
    )
    detector.add_on_recovery_callback(refresher.on_connectivity_recovered)
    bring_stub_network_back(detector, prober)

    refresher(agent_id)

    assert _refreshed_agent_ids(client_queue) == [str(agent_id)]


def test_quitting_inside_the_settle_window_drops_the_held_refresh() -> None:
    """The worker waits on the group's shutdown event, not on the clock.

    A settle long enough that waiting it out would trip the suite's own timeout,
    so this can only pass by the shutdown being what ends the wait.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-shutdown") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=600.0,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)
        refresher(agent_id)
        bring_stub_network_back(detector, prober)
        concurrency_group.shutdown()

    # The held refresh went with it, which is correct: there is no window left
    # to repaint, and the next launch loads the workspace fresh anyway.
    assert _refreshed_agent_ids(client_queue) == []
