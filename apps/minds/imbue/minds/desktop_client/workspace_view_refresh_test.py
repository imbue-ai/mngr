"""Unit coverage for the gate that decides *when* a recovered machine's view is refreshed.

The refresh itself is one published frame and is covered by the publisher's own
tests. What is worth pinning here is the timing rule, because the bug it exists
for is invisible to every other check: the frame publishes, the window obeys it,
the reload commits -- and the page is blank anyway, because the reload was
issued into a network that was still coming back.
"""

import queue
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final

import pytest

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
from imbue.minds.desktop_client.testing import make_sleep_tracker
from imbue.minds.desktop_client.testing import record_sleep_of
from imbue.minds.desktop_client.workspace_view_refresh import WorkspaceViewRefresher
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_until

# Long enough that "nothing has published yet" cannot pass by being quick, and
# that a settle worker still waiting is unmistakably still waiting.
_SETTLE_SECONDS: Final[float] = 0.5

# For the one test that has to place an edge *inside* a window and then tell the
# two deadlines apart afterwards. Long enough that half of it is unambiguously
# before the first worker comes round, at the cost of a slower test, so the rest
# of the file keeps the short settle above.
_SUPERSEDED_SETTLE_SECONDS: Final[float] = 2.0

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


def _wait_for_refresh_frames(client_queue: "queue.Queue[str | None]") -> list[dict[str, Any]]:
    """Poll until a refresh frame lands, and answer with the refresh frames seen.

    The drain is destructive, so what a pass reads has to be accumulated rather
    than re-read once the poll returns.
    """
    refreshes: list[dict[str, Any]] = []

    def _has_refreshed() -> bool:
        refreshes.extend(
            frame for frame in drain_ui_channel_frames(client_queue) if frame["type"] == "workspace_refresh"
        )
        return bool(refreshes)

    assert poll_until(_has_refreshed, timeout=_PUBLISH_WAIT_SECONDS), "the held view refresh was never published"
    return refreshes


def _wait_for_refreshes(client_queue: "queue.Queue[str | None]") -> list[str]:
    """Poll until a refresh frame lands, and answer with the ids seen."""
    return [frame["agent_id"] for frame in _wait_for_refresh_frames(client_queue)]


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


def test_a_settle_worker_whose_window_was_superseded_leaves_the_publish_to_the_newer_one() -> None:
    """A second recovery inside the first settle moves the deadline out; it does not keep the old one.

    The older worker's wait expires while the newer window still has seconds to
    run, on a device that by then reads perfectly fine and has not slept -- so
    nothing but the window's own identity stops it publishing a settle early,
    into the interface the newer recovery says is still coming back.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    with ConcurrencyGroup(name="test-view-refresh-superseded") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            concurrency_group=concurrency_group,
            settle_seconds=_SUPERSEDED_SETTLE_SECONDS,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)

        refresher(agent_id)
        started_at = time.monotonic()
        bring_stub_network_back(detector, prober)

        # Nothing publishes in the first half of the window -- which is also
        # how this test reaches the middle of it, so the edge below is
        # unmistakably one the first worker is still waiting through.
        assert not poll_until(
            lambda: bool(_refreshed_agent_ids(client_queue)),
            timeout=_SUPERSEDED_SETTLE_SECONDS / 2,
            poll_interval=0.02,
        )
        # The network flaps again: down and back is another bad -> good edge,
        # and so another window.
        prober.reachable_hosts = set()
        prober.ssh_endpoints = set()
        detector.probe_now()
        bring_stub_network_back(detector, prober)

        assert _wait_for_refreshes(client_queue) == [str(agent_id)]
        # Half a settle plus a whole one, against the single settle the
        # superseded worker would have published on.
        assert time.monotonic() - started_at >= _SUPERSEDED_SETTLE_SECONDS * 1.4


def test_a_settle_the_device_slept_through_is_waited_out_again_from_the_wake() -> None:
    """The incident's shape: the laptop slept inside the settle, and the wait ended at the wake.

    A timed wait can return the instant the machine is back -- exactly the
    interface transition the settle exists to outlast. The worker establishes
    the wake for itself rather than racing the heartbeat for it, and stands
    down for the window that wake opens.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    sleep_tracker, clock = make_sleep_tracker()
    with ConcurrencyGroup(name="test-view-refresh-slept-through") as concurrency_group:
        detector, prober = build_stub_connectivity_detector(concurrency_group, is_internet_up=False)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            sleep_tracker=sleep_tracker,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        detector.add_on_recovery_callback(refresher.on_connectivity_recovered)
        sleep_tracker.add_on_wake_callback(refresher.on_wake)

        # The last heartbeat anyone recorded is long before the settle begins,
        # so the worker's own tick at the end of its wait is what closes the
        # gap -- the heartbeat loop has not got there first.
        clock.lag_seconds = 700.0
        sleep_tracker.record_heartbeat()
        clock.lag_seconds = 0.0

        refresher(agent_id)
        started_at = time.monotonic()
        bring_stub_network_back(detector, prober)

        # One settle is not enough: the first one was slept through.
        assert not poll_until(
            lambda: bool(_refreshed_agent_ids(client_queue)), timeout=_SETTLE_SECONDS * 1.5, poll_interval=0.02
        )
        assert _wait_for_refreshes(client_queue) == [str(agent_id)]
        assert time.monotonic() - started_at >= _SETTLE_SECONDS * 2


def test_a_refresh_raised_just_after_a_wake_waits_out_a_settle() -> None:
    """A wake is a network transition for the settle's purposes, whatever the reading says.

    The reading after a wake is UNKNOWN, which holds nothing on its own; the
    wake itself opens the window, so a machine whose recovery lands in the
    seconds after the lid opens is not reloaded into the interface coming up.
    """
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    sleep_tracker, clock = make_sleep_tracker()
    with ConcurrencyGroup(name="test-view-refresh-after-wake") as concurrency_group:
        detector, _prober = build_stub_connectivity_detector(concurrency_group)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            sleep_tracker=sleep_tracker,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        sleep_tracker.add_on_wake_callback(refresher.on_wake)

        started_at = time.monotonic()
        record_sleep_of(sleep_tracker, clock, seconds=700.0)
        refresher(agent_id)
        assert _refreshed_agent_ids(client_queue) == []

        assert _wait_for_refreshes(client_queue) == [str(agent_id)]
        assert time.monotonic() - started_at >= _SETTLE_SECONDS


def _published_refresh_cutoffs(client_queue: "queue.Queue[str | None]") -> list[datetime | None]:
    """The ``loaded_before`` of every refresh frame published so far, polled until at least one lands."""
    return [
        None if frame["loaded_before"] is None else datetime.fromisoformat(frame["loaded_before"])
        for frame in _wait_for_refresh_frames(client_queue)
    ]


def _an_hour_ago() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.mark.parametrize(
    ("is_sleep_recorded", "is_run_start_readable", "is_refresh_published"),
    [(True, True, True), (False, True, False), (False, False, True)],
    ids=["slept-during-the-run", "awake-throughout", "run-start-unreadable"],
)
def test_an_update_repeats_its_view_refresh_only_on_a_device_that_may_have_slept_through_it(
    is_sleep_recorded: bool, is_run_start_readable: bool, is_refresh_published: bool
) -> None:
    """The apply refreshed every view as it landed; only a device that could have missed that is owed a repeat."""
    agent_id = AgentId.generate()
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    publisher, client_queue = build_ui_state_publisher_for_test()
    sleep_tracker, clock = make_sleep_tracker()
    with ConcurrencyGroup(name="test-view-refresh-update-sleep-gate") as concurrency_group:
        refresher = WorkspaceViewRefresher(
            publisher=publisher, sleep_tracker=sleep_tracker, concurrency_group=concurrency_group
        )
        if is_sleep_recorded:
            record_sleep_of(sleep_tracker, clock, seconds=1500.0)

        refresher.refresh_views_after_update(
            agent_id, run_started_at=_an_hour_ago() if is_run_start_readable else None, ended_at=ended_at
        )

        assert _refreshed_agent_ids(client_queue) == ([str(agent_id)] if is_refresh_published else [])


@pytest.mark.parametrize("is_run_start_readable", [True, False], ids=["run-start-readable", "run-start-unreadable"])
def test_an_update_verdict_seen_just_after_a_wake_is_held_and_keeps_its_cutoff(is_run_start_readable: bool) -> None:
    """The laptop slept through the update, and the app reads its verdict seconds after the lid opens.

    The heartbeat loop has not recorded the wake yet, so it is the verdict's
    own reading that does. The reload waits out the wake's settle like any
    other, and still reaches the windows only as "reload a page loaded before
    the run ended".
    """
    agent_id = AgentId.generate()
    ended_at = datetime(2026, 9, 28, 20, 14, 49, 432000, tzinfo=timezone.utc)
    publisher, client_queue = build_ui_state_publisher_for_test()
    sleep_tracker, clock = make_sleep_tracker()
    with ConcurrencyGroup(name="test-view-refresh-verdict-after-wake") as concurrency_group:
        detector, _prober = build_stub_connectivity_detector(concurrency_group)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            sleep_tracker=sleep_tracker,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        sleep_tracker.add_on_wake_callback(refresher.on_wake)

        started_at = time.monotonic()
        clock.lag_seconds = 988.0
        sleep_tracker.record_heartbeat()
        clock.lag_seconds = 0.0
        refresher.refresh_views_after_update(
            agent_id, run_started_at=_an_hour_ago() if is_run_start_readable else None, ended_at=ended_at
        )
        assert _refreshed_agent_ids(client_queue) == []

        assert _published_refresh_cutoffs(client_queue) == [ended_at]
        assert time.monotonic() - started_at >= _SETTLE_SECONDS


_EARLIER_RUN_END: Final[datetime] = datetime(2026, 9, 28, 19, 2, 11, tzinfo=timezone.utc)
_LATER_RUN_END: Final[datetime] = datetime(2026, 9, 28, 20, 14, 49, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("first_cutoff", "second_cutoff", "expected_cutoff"),
    [
        (_LATER_RUN_END, None, None),
        (None, _LATER_RUN_END, None),
        (_EARLIER_RUN_END, _LATER_RUN_END, _LATER_RUN_END),
        (_LATER_RUN_END, _EARLIER_RUN_END, _LATER_RUN_END),
    ],
    ids=["verdict-then-recovery", "recovery-then-verdict", "earlier-then-later", "later-then-earlier"],
)
def test_two_refreshes_held_for_one_machine_publish_the_one_that_reloads_more(
    first_cutoff: datetime | None, second_cutoff: datetime | None, expected_cutoff: datetime | None
) -> None:
    """One frame per machine leaves the hold, and it must reload every page either refresh would have."""
    agent_id = AgentId.generate()
    publisher, client_queue = build_ui_state_publisher_for_test()
    sleep_tracker, clock = make_sleep_tracker()
    with ConcurrencyGroup(name="test-view-refresh-merge") as concurrency_group:
        detector, _prober = build_stub_connectivity_detector(concurrency_group)
        detector.probe_now()
        refresher = WorkspaceViewRefresher(
            publisher=publisher,
            connectivity_detector=detector,
            sleep_tracker=sleep_tracker,
            concurrency_group=concurrency_group,
            settle_seconds=_SETTLE_SECONDS,
        )
        sleep_tracker.add_on_wake_callback(refresher.on_wake)

        record_sleep_of(sleep_tracker, clock, seconds=711.0)
        for cutoff in (first_cutoff, second_cutoff):
            if cutoff is None:
                refresher(agent_id)
            else:
                refresher.refresh_views_after_update(agent_id, run_started_at=_an_hour_ago(), ended_at=cutoff)

        assert _published_refresh_cutoffs(client_queue) == [expected_cutoff]


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
