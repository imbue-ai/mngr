import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest

from imbue.minds.desktop_client.create_progress import ETA_SHORTEN_HOLD_SECONDS
from imbue.minds.desktop_client.create_progress import OperationProgressTracker
from imbue.minds.desktop_client.create_progress import ProgressStepStatus
from imbue.minds.desktop_client.create_progress import apply_bootstrap_event_lines
from imbue.minds.desktop_client.create_progress import parse_bootstrap_event_timestamp
from imbue.minds.desktop_client.create_progress import remaining_text_for
from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.progress import ProgressStep
from imbue.mngr.utils.progress import parse_progress_event


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _plan(*pairs: tuple[str, str]) -> list[ProgressStep]:
    return [ProgressStep(step_id=ProgressStepId(step_id), label=label) for step_id, label in pairs]


def _tracker(clock: _FakeClock, **expected: float) -> OperationProgressTracker:
    return OperationProgressTracker(expected_seconds_by_step_id=expected, default_expected_seconds=10.0, clock=clock)


def _mngr_line(**fields: object) -> dict[str, object]:
    return json.loads(json.dumps(fields))


def test_percent_is_the_expected_duration_weighted_share_of_completed_steps() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, clone=10.0, build=80.0, ready=10.0)
    tracker.announce_plan(_plan(("clone", "Cloning"), ("build", "Building"), ("ready", "Waiting")), None)

    assert tracker.snapshot().percent == 0.0
    tracker.start_step("clone", "Cloning", None, None)
    tracker.complete_step("clone")
    assert tracker.snapshot().percent == pytest.approx(10.0)
    tracker.start_step("build", "Building", None, 4)
    tracker.advance_step("build", 2, 4)
    assert tracker.snapshot().percent == pytest.approx(50.0)
    tracker.complete_step("build")
    tracker.start_step("ready", "Waiting", None, None)
    tracker.complete_step("ready")

    assert tracker.snapshot().percent == 100.0
    assert [step.status for step in tracker.snapshot().steps] == [ProgressStepStatus.DONE] * 3


def test_running_step_without_a_counter_reads_linearly_then_holds_and_never_claims_completion() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, only=100.0)
    tracker.announce_plan(_plan(("only", "Only step")), None)
    tracker.start_step("only", "Only step", None, None)

    clock.now += 40
    assert tracker.snapshot().percent == pytest.approx(40.0)
    # Two thirds of the way through its expectation the overrun margin takes
    # over, and the bar holds there rather than creeping toward done.
    clock.now += 40
    assert tracker.snapshot().percent == pytest.approx(66.7, abs=0.1)
    clock.now += 1000
    assert tracker.snapshot().percent == pytest.approx(66.7, abs=0.1)


def test_a_step_learned_at_zero_seconds_still_snapshots_while_it_runs() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, instant=0.0)
    tracker.announce_plan(_plan(("parent", "Parent")), None)
    tracker.start_step("instant", "Instant", "parent", None)

    for _ in range(2):
        snapshot = tracker.snapshot()
        assert 0.0 <= snapshot.percent < 100.0
        assert snapshot.current_label == "Instant"
        clock.now += 1


def test_percent_never_moves_backwards_even_when_a_step_restarts() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, first=10.0, second=10.0)
    tracker.announce_plan(_plan(("first", "First"), ("second", "Second")), None)
    tracker.start_step("first", "First", None, None)
    clock.now += 6
    before = tracker.snapshot().percent
    assert before == pytest.approx(30.0)

    tracker.start_step("first", "First", None, None)

    assert tracker.snapshot().percent == before


def test_starting_a_later_planned_step_completes_the_earlier_ones() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    tracker.announce_plan(_plan(("a", "A"), ("b", "B"), ("c", "C")), None)

    tracker.start_step("c", "C", None, None)

    assert [step.status for step in tracker.snapshot().steps] == [
        ProgressStepStatus.DONE,
        ProgressStepStatus.DONE,
        ProgressStepStatus.RUNNING,
    ]


def test_mngr_events_nest_under_the_desktop_step_and_providers_nest_under_mngr_phases() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, clone=10.0, mngr=90.0, creating_host=60.0, preparing=5.0)
    tracker.announce_plan(_plan(("clone", "Cloning"), ("mngr", "Creating")), None)
    tracker.start_step("clone", "Cloning", None, None)
    tracker.complete_step("clone")
    tracker.start_step("mngr", "Creating", None, None)

    for line in (
        _mngr_line(
            kind="plan",
            steps=[{"step": "preparing", "label": "Preparing"}, {"step": "creating_host", "label": "Host"}],
        ),
        _mngr_line(kind="started", step="preparing", label="Preparing"),
        _mngr_line(kind="completed", step="preparing", elapsed_seconds=1.0),
        _mngr_line(kind="started", step="creating_host", label="Creating the host"),
        _mngr_line(kind="started", step="docker.build", parent="creating_host", label="Building the image", total=10),
        _mngr_line(kind="advanced", step="docker.build", completed=5, total=10),
    ):
        event = parse_progress_event(line)
        assert event is not None
        tracker.apply_event(event, "mngr")

    snapshot = tracker.snapshot()
    assert snapshot.current_label == "Building the image"
    # clone (10) is done; under mngr (90): preparing (5) done, creating_host
    # (60) half done through its only known child (the build, default weight 10
    # out of the host's 60). The desktop's step list stays the top level only.
    assert [step.step_id for step in snapshot.steps] == ["clone", "mngr"]
    expected_mngr_fraction = (5.0 + 60.0 * (10.0 * 0.5 / 60.0)) / 90.0
    assert snapshot.percent == pytest.approx(100 * (10.0 + 90.0 * expected_mngr_fraction) / 100.0)
    # A completion mngr timed is recorded with mngr's own measurement.
    assert ("preparing", 1.0) in [(duration.step_id, duration.seconds) for duration in tracker.drain_durations()]


def test_skipped_steps_count_as_done_but_measure_nothing() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, build=100.0)
    tracker.announce_plan(_plan(("build", "Building")), None)
    tracker.start_step("build", "Building", None, None)
    started = parse_progress_event(
        _mngr_line(kind="started", step="build.run_apt_1a2b3c", parent="build", label="Building stage 2/4")
    )
    skipped = parse_progress_event(
        _mngr_line(kind="skipped", step="build.run_apt_1a2b3c", parent="build", label="Building stage 2/4")
    )
    never_started = parse_progress_event(
        _mngr_line(kind="skipped", step="build.copy_app_7de3ae", parent="build", label="Building stage 3/4")
    )
    assert started is not None and skipped is not None and never_started is not None

    tracker.apply_event(started, None)
    clock.now += 5
    tracker.apply_event(skipped, None)
    tracker.apply_event(never_started, None)

    assert dict(_children_statuses(tracker, "build")) == {
        "build.run_apt_1a2b3c": ProgressStepStatus.DONE,
        "build.copy_app_7de3ae": ProgressStepStatus.DONE,
    }
    assert tracker.drain_durations() == []
    assert tracker.drain_skipped_step_ids() == ["build.run_apt_1a2b3c", "build.copy_app_7de3ae"]
    assert tracker.drain_skipped_step_ids() == []


def test_re_announced_plan_keeps_completed_steps_and_resets_on_restart() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    tracker.announce_plan(_plan(("preparing", "Preparing"), ("creating_host", "Host")), "mngr")
    tracker.start_step("preparing", "Preparing", "mngr", None)
    tracker.complete_step("preparing")

    tracker.announce_plan(_plan(("preparing", "Preparing"), ("creating_host", "Host"), ("extra", "Extra")), "mngr")
    mngr_children = {step_id: status for step_id, status in _children_statuses(tracker, "mngr")}
    assert mngr_children["preparing"] is ProgressStepStatus.DONE
    assert mngr_children["extra"] is ProgressStepStatus.PENDING

    tracker.start_step("preparing", "Preparing", "mngr", None)
    assert dict(_children_statuses(tracker, "mngr"))["preparing"] is ProgressStepStatus.RUNNING


def _children_statuses(tracker: OperationProgressTracker, parent_id: str) -> list[tuple[str, ProgressStepStatus]]:
    parent = tracker._node_by_id[parent_id]
    return [(child.step_id, child.status) for child in parent.children]


def test_rate_and_cap_let_a_client_extrapolate_only_through_the_running_step() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, first=50.0, second=50.0)
    tracker.announce_plan(_plan(("first", "First"), ("second", "Second")), None)
    tracker.start_step("first", "First", None, None)

    snapshot = tracker.snapshot()

    # The first step owns half the bar and reads linearly over 50s, so the bar
    # grows at one percent per second and may be extrapolated to two thirds of
    # its share, where the overrun margin takes over.
    assert snapshot.percent_per_second == pytest.approx(1.0)
    assert snapshot.percent_cap == pytest.approx(100 / 3)
    tracker.start_step("second", "Second", None, 4)
    counted = tracker.snapshot()
    assert counted.percent_per_second == 0.0
    assert counted.percent_cap == counted.percent


def test_remaining_text_buckets_round_up() -> None:
    assert remaining_text_for(5) == "Almost there..."
    assert remaining_text_for(30) == "Less than a minute remaining"
    assert remaining_text_for(70) == "About a minute remaining"
    assert remaining_text_for(80) == "About 2 minutes remaining"
    assert remaining_text_for(130) == "About 3 minutes remaining"
    assert remaining_text_for(600) == "About 10 minutes remaining"


def test_remaining_text_lengthens_at_once_but_shortens_only_after_a_hold() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, slow=200.0)
    tracker.announce_plan(_plan(("slow", "Slow")), None)
    tracker.start_step("slow", "Slow", None, None)
    assert tracker.snapshot().remaining_text == "About 4 minutes remaining"

    # 100s in, the estimate is 100s, but the shorter wording waits for the
    # estimate to persist.
    clock.now += 100
    assert tracker.snapshot().remaining_text == "About 4 minutes remaining"
    clock.now += ETA_SHORTEN_HOLD_SECONDS
    assert tracker.snapshot().remaining_text == "About 2 minutes remaining"

    # The step restarts, so the live estimate jumps back up, and the text says so at once.
    tracker.start_step("slow", "Slow", None, None)
    assert tracker.snapshot().remaining_text == "About 4 minutes remaining"


def test_a_step_that_overruns_its_expectation_is_read_as_needing_half_again_what_it_took() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, build=30.0)
    tracker.announce_plan(_plan(("build", "Build")), None)
    tracker.start_step("build", "Build", None, None)

    clock.now += 400
    snapshot = tracker.snapshot()

    # 400s into a 30s expectation: at least 200s more, not "almost there".
    assert snapshot.remaining_text == "About 4 minutes remaining"
    assert snapshot.percent == pytest.approx(66.7, abs=0.1)


def test_a_counted_step_is_never_read_as_faster_than_its_observed_pace() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, build=36.0)
    tracker.announce_plan(_plan(("build", "Build")), None)
    tracker.start_step("build", "Build", None, 36)

    # 12 of 36 stages in 120s: the expectation says 24s are left, the pace says 240s.
    clock.now += 120
    tracker.advance_step("build", 12, 36)

    assert tracker.snapshot().remaining_text == "About 4 minutes remaining"


def test_a_parent_whose_children_are_all_done_but_still_runs_lengthens_the_estimate() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, build=100.0, stage=90.0)
    tracker.announce_plan(_plan(("build", "Build")), None)
    tracker.start_step("build", "Build", None, None)
    tracker.start_step("stage", "Stage", "build", None)
    clock.now += 90
    tracker.complete_step("stage")
    # Its only child is done, so only the 10s the child did not account for is left.
    assert tracker.snapshot().remaining_text == "Almost there..."

    # Work that no child counts runs on past the build's expectation, and the
    # estimate lengthens with the overrun instead of ending.
    clock.now += 200
    assert tracker.snapshot().remaining_text == "About 2 minutes remaining"
    assert tracker.snapshot().percent < 100.0


def test_children_that_finish_early_leave_their_parent_nearly_done_whatever_it_expected() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, host=400.0, build=390.0, start=5.0)
    tracker.announce_plan(_plan(("host", "Host")), None)
    tracker.start_step("host", "Host", None, None)
    tracker.announce_plan(_plan(("build", "Build"), ("start", "Start")), "host")
    tracker.start_step("build", "Build", "host", None)
    # A warm cache finishes the build in a quarter of its expectation.
    clock.now += 100
    tracker.complete_step("build")

    # 5s of the pending child plus the 5s the children do not account for; not the 300s the host "has left".
    assert tracker.snapshot().remaining_text == "Almost there..."


def test_a_running_child_decides_its_parents_estimate() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, build=400.0, slow_stage=300.0, fast_stage=10.0)
    tracker.announce_plan(_plan(("build", "Build")), None)
    tracker.start_step("build", "Build", None, None)
    tracker.announce_plan(_plan(("slow_stage", "Slow"), ("fast_stage", "Fast")), "build")
    tracker.start_step("slow_stage", "Slow", "build", None)
    clock.now += 500
    tracker.complete_step("slow_stage")
    tracker.start_step("fast_stage", "Fast", "build", None)

    # The build is 100s past its own expectation, but its running child says
    # 10s (plus the 90s of the build its known children do not account for).
    assert tracker.snapshot().remaining_text == "About 2 minutes remaining"


def test_complete_all_finishes_every_step_and_drains_durations_once() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    tracker.announce_plan(_plan(("a", "A"), ("b", "B")), None)
    tracker.start_step("a", "A", None, None)
    clock.now += 3
    tracker.start_step("b", "B", None, None)
    clock.now += 4

    tracker.complete_all()

    snapshot = tracker.snapshot()
    assert snapshot.percent == 100.0
    assert snapshot.remaining_text == ""
    assert [(duration.step_id, duration.seconds) for duration in tracker.drain_durations()] == [("a", 3.0), ("b", 4.0)]
    assert tracker.drain_durations() == []


def test_events_for_unknown_steps_are_tolerated() -> None:
    tracker = _tracker(_FakeClock())

    tracker.advance_step("ghost", 1, 2)
    tracker.complete_step("ghost")
    event = parse_progress_event(
        {"kind": "started", "step": "orphan.child", "parent": "never_announced", "label": "x"}
    )
    assert event is not None
    tracker.apply_event(event, None)

    snapshot = tracker.snapshot()
    assert snapshot.current_label == "x"
    assert [step.step_id for step in snapshot.steps] == ["never_announced"]


# Lines as the template's bootstrap writes them (system/libs/bootstrap/src/bootstrap/manager.py), one per event.
_BOOTSTRAP_LINES = [
    '{"timestamp": "2026-10-18T01:30:00.123456000Z", "type": "bootstrap_step_started", "event_id": "boot-1", "source": "bootstrap", "step": "sync_venv", "label": "Installing the workspace\'s Python packages"}',
    "not json at all",
    '{"timestamp": "2026-10-18T01:30:09.000000000Z", "type": "bootstrap_step_completed", "event_id": "boot-2", "source": "bootstrap", "step": "sync_venv", "label": "Installing the workspace\'s Python packages", "duration_seconds": 8.75}',
    '{"timestamp": "2026-10-18T01:30:09.500000000Z", "type": "bootstrap_step_started", "event_id": "boot-3", "source": "bootstrap", "step": "install_cron", "label": "Installing scheduled jobs"}',
    '{"timestamp": "2026-10-18T01:30:10.000000000Z", "type": "bootstrap_completed", "event_id": "boot-4", "source": "bootstrap", "step": "supervisord", "label": "Starting the workspace\'s services"}',
    '{"timestamp": "2026-10-18T01:30:10.000000000Z", "type": "bootstrap_something_newer", "event_id": "boot-5", "source": "bootstrap", "step": "later", "label": "Later"}',
]


def test_bootstrap_event_lines_before_the_create_belong_to_an_earlier_boot_and_are_ignored() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, wait_for_ready=60.0)
    tracker.announce_plan(_plan(("wait_for_ready", "Waiting")), None)
    tracker.start_step("wait_for_ready", "Waiting", None, None)
    # The create began a minute after the events, within the skew allowance, so they are the live boot's.
    create_started_at = datetime(2026, 10, 18, 1, 31, 0, tzinfo=timezone.utc)

    apply_bootstrap_event_lines(tracker, _BOOTSTRAP_LINES, "wait_for_ready", not_before=create_started_at)

    assert tracker.snapshot().current_label == "Installing scheduled jobs"
    assert [(duration.step_id, duration.seconds) for duration in tracker.drain_durations()] == [
        ("bootstrap.sync_venv", 8.75)
    ]
    # Past the allowance, every one of those events is an earlier boot's.
    later = _tracker(clock, wait_for_ready=60.0)
    later.announce_plan(_plan(("wait_for_ready", "Waiting")), None)
    later.start_step("wait_for_ready", "Waiting", None, None)
    apply_bootstrap_event_lines(
        later, _BOOTSTRAP_LINES, "wait_for_ready", not_before=create_started_at + timedelta(minutes=20)
    )
    assert later.snapshot().current_label == "Waiting"
    assert later.drain_durations() == []


def test_parse_bootstrap_event_timestamp_reads_nanosecond_utc_stamps_only() -> None:
    assert parse_bootstrap_event_timestamp("2026-10-18T01:30:00.123456789Z") == datetime(
        2026, 10, 18, 1, 30, 0, 123456, tzinfo=timezone.utc
    )
    assert parse_bootstrap_event_timestamp("2026-10-18T01:30:00+00:00") is None
    assert parse_bootstrap_event_timestamp("not a time") is None
    assert parse_bootstrap_event_timestamp(1760750000) is None


def test_bootstrap_event_lines_nest_under_the_wait_step_and_keep_the_bootstrap_timings() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock, wait_for_ready=60.0)
    tracker.announce_plan(_plan(("wait_for_ready", "Waiting for the workspace to boot")), None)
    tracker.start_step("wait_for_ready", "Waiting for the workspace to boot", None, None)

    apply_bootstrap_event_lines(tracker, _BOOTSTRAP_LINES[:3], "wait_for_ready")
    clock.now += 100
    apply_bootstrap_event_lines(tracker, _BOOTSTRAP_LINES[3:], "wait_for_ready")

    snapshot = tracker.snapshot()
    assert snapshot.current_label == "Installing scheduled jobs"
    assert dict(_children_statuses(tracker, "wait_for_ready")) == {
        "bootstrap.sync_venv": ProgressStepStatus.DONE,
        "bootstrap.install_cron": ProgressStepStatus.RUNNING,
    }
    # The sync step's duration is the bootstrap's own 8.75s, not the 100s that passed on this side.
    assert [(duration.step_id, duration.seconds) for duration in tracker.drain_durations()] == [
        ("bootstrap.sync_venv", 8.75)
    ]
