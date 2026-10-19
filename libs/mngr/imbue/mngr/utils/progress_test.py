import json

import pytest
from inline_snapshot import snapshot

from imbue.mngr.primitives import CreatePhase
from imbue.mngr.primitives import InvalidProgressStepIdError
from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.progress import NestedProgressReporter
from imbue.mngr.utils.progress import NullProgressReporter
from imbue.mngr.utils.progress import ProgressEvent
from imbue.mngr.utils.progress import ProgressEventKind
from imbue.mngr.utils.progress import ProgressEventSinkReporter
from imbue.mngr.utils.progress import ProgressStep
from imbue.mngr.utils.progress import command_step_id
from imbue.mngr.utils.progress import parse_progress_event
from imbue.mngr.utils.progress import summarize_command_for_label
from imbue.mngr.utils.progress import text_step_id


class _FakeClock:
    """A clock that only moves when the test advances it."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_progress_step_id_accepts_dotted_lowercase_words_and_rejects_the_rest() -> None:
    assert ProgressStepId("creating_host") == "creating_host"
    assert ProgressStepId("docker.build") == "docker.build"
    assert ProgressStepId("provisioning.command_3") == "provisioning.command_3"
    for bad in ("", "Docker.Build", "docker build", ".build", "docker..build", "docker-build"):
        with pytest.raises(InvalidProgressStepIdError):
            ProgressStepId(bad)


def test_create_phase_step_ids_are_lowercase_member_names() -> None:
    assert CreatePhase.CREATING_HOST.step_id == ProgressStepId("creating_host")
    assert [phase.step_id for phase in CreatePhase] == snapshot(
        ["preparing", "creating_host", "setting_up_host", "transferring_project", "provisioning", "starting_agent"]
    )


def test_sink_reporter_emits_plan_started_advanced_and_completed_with_elapsed() -> None:
    clock = _FakeClock()
    events: list[ProgressEvent] = []
    reporter = ProgressEventSinkReporter(emit=events.append, clock=clock)
    build = ProgressStepId("docker.build")

    reporter.announce_plan([ProgressStep(step_id=ProgressStepId("preparing"), label="Preparing")], None)
    reporter.start_step(build, "Building the image", CreatePhase.CREATING_HOST.step_id, 9)
    reporter.advance_step(build, 3, 9)
    clock.now += 12.5
    reporter.complete_step(build)

    assert [event.to_wire() for event in events] == snapshot(
        [
            {"kind": "plan", "steps": [{"step": "preparing", "label": "Preparing"}]},
            {
                "kind": "started",
                "step": "docker.build",
                "parent": "creating_host",
                "label": "Building the image",
                "total": 9,
            },
            {"kind": "advanced", "step": "docker.build", "completed": 3, "total": 9},
            {"kind": "completed", "step": "docker.build", "elapsed_seconds": 12.5},
        ]
    )


def test_sink_reporter_completion_of_an_unstarted_step_carries_no_elapsed() -> None:
    events: list[ProgressEvent] = []
    reporter = ProgressEventSinkReporter(emit=events.append)

    reporter.complete_step(ProgressStepId("never_started"))

    assert events[0].elapsed_seconds is None
    assert "elapsed_seconds" not in events[0].to_wire()


def test_step_context_manager_completes_only_on_normal_exit() -> None:
    events: list[ProgressEvent] = []
    reporter = ProgressEventSinkReporter(emit=events.append)

    with reporter.step(ProgressStepId("ok"), "Fine"):
        pass
    with pytest.raises(ValueError, match="boom 41731"):
        with reporter.step(ProgressStepId("broken"), "Breaks"):
            raise ValueError("boom 41731")

    assert [(event.kind, str(event.step_id)) for event in events] == [
        (ProgressEventKind.STARTED, "ok"),
        (ProgressEventKind.COMPLETED, "ok"),
        (ProgressEventKind.STARTED, "broken"),
    ]


def test_null_reporter_accepts_every_call_silently() -> None:
    reporter = NullProgressReporter()
    reporter.announce_plan([], None)
    with reporter.step(ProgressStepId("anything"), "Anything", None, 3):
        reporter.advance_step(ProgressStepId("anything"), 1, 3)
    reporter.skip_step(ProgressStepId("cached"), "Cached", None)


def test_wire_round_trip_preserves_every_field() -> None:
    event = ProgressEvent(
        kind=ProgressEventKind.STARTED,
        step_id=ProgressStepId("vps.build_image"),
        parent_id=ProgressStepId("creating_host"),
        label="Building",
        total=4,
    )

    parsed = parse_progress_event(json.loads(json.dumps(event.to_wire())))

    assert parsed == event


def test_parse_progress_event_tolerates_unknown_kinds_and_malformed_fields() -> None:
    assert parse_progress_event({"kind": "teleported", "step": "x"}) is None
    assert parse_progress_event({"kind": "started"}) is None
    assert parse_progress_event({"kind": "started", "step": "Not Valid"}) is None
    parsed = parse_progress_event(
        {"kind": "advanced", "step": "docker.build", "completed": -1, "total": "nine", "elapsed_seconds": True}
    )
    assert parsed is not None
    assert (parsed.completed, parsed.total, parsed.elapsed_seconds) == (None, None, None)


def test_parse_progress_event_plan_skips_entries_it_cannot_read() -> None:
    parsed = parse_progress_event(
        {"kind": "plan", "steps": [{"step": "preparing", "label": "Preparing"}, {"label": "no id"}, "junk"]}
    )

    assert parsed is not None
    assert parsed.steps == (ProgressStep(step_id=ProgressStepId("preparing"), label="Preparing"),)


def test_skip_step_emits_a_skipped_event_with_its_label_and_parent_and_no_elapsed() -> None:
    clock = _FakeClock()
    events: list[ProgressEvent] = []
    reporter = ProgressEventSinkReporter(emit=events.append, clock=clock)
    stage = ProgressStepId("docker.build.run_apt_1a2b3c")

    reporter.start_step(stage, "Building stage 2/4", ProgressStepId("docker.build"), None)
    clock.now += 5
    reporter.skip_step(stage, "Building stage 2/4", ProgressStepId("docker.build"))
    reporter.complete_step(stage)

    assert [event.to_wire() for event in events[1:]] == snapshot(
        [
            {
                "kind": "skipped",
                "step": "docker.build.run_apt_1a2b3c",
                "parent": "docker.build",
                "label": "Building stage 2/4",
            },
            {"kind": "completed", "step": "docker.build.run_apt_1a2b3c"},
        ]
    )
    parsed = parse_progress_event(json.loads(json.dumps(events[1].to_wire())))
    assert parsed == events[1]


def test_text_step_id_names_a_step_by_its_text_and_keeps_the_id_when_only_other_text_changes() -> None:
    build = ProgressStepId("docker.build")
    run_make = text_step_id(build, "RUN make -j4 && make install")

    assert str(run_make) == snapshot("docker.build.run_make_j4_8611c5")
    assert text_step_id(build, "RUN make -j4 && make install") == run_make
    assert text_step_id(build, "RUN make -j8 && make install") != run_make
    assert str(text_step_id(build, "   ")).startswith("docker.build.step_")
    assert len(str(text_step_id(build, "COPY " + "x" * 200 + " /app"))) < 60


def test_summarize_command_for_label_collapses_whitespace_and_truncates() -> None:
    assert summarize_command_for_label("  bash   system/scripts/setup.sh\n") == "bash system/scripts/setup.sh"
    long_command = "x" * 100
    summarized = summarize_command_for_label(long_command, max_length=20)
    assert summarized == "x" * 17 + "..."
    assert len(summarized) == 20


def test_command_step_id_nests_under_its_parent() -> None:
    assert command_step_id(ProgressStepId("provisioning"), 2) == ProgressStepId("provisioning.command_2")


def test_nested_reporter_re_homes_only_the_steps_under_the_outer_parent() -> None:
    events: list[ProgressEvent] = []
    reporter = NestedProgressReporter(
        inner=ProgressEventSinkReporter(emit=events.append),
        outer_parent_id=CreatePhase.CREATING_HOST.step_id,
        nested_parent_id=ProgressStepId("imbue_cloud.rebuild"),
    )
    build = ProgressStepId("vps.build_image")

    reporter.announce_plan([ProgressStep(step_id=build, label="Building")], CreatePhase.CREATING_HOST.step_id)
    reporter.announce_plan([ProgressStep(step_id=ProgressStepId("preparing"), label="Preparing")], None)
    with reporter.step(build, "Building", CreatePhase.CREATING_HOST.step_id, 4):
        reporter.advance_step(build, 2, 4)
    reporter.start_step(ProgressStepId("other"), "Other", CreatePhase.PROVISIONING.step_id, None)
    reporter.skip_step(ProgressStepId("cached"), "Cached", CreatePhase.CREATING_HOST.step_id)

    assert [(event.kind, event.step_id, event.parent_id) for event in events] == [
        (ProgressEventKind.PLAN, None, ProgressStepId("imbue_cloud.rebuild")),
        (ProgressEventKind.PLAN, None, None),
        (ProgressEventKind.STARTED, build, ProgressStepId("imbue_cloud.rebuild")),
        (ProgressEventKind.ADVANCED, build, None),
        (ProgressEventKind.COMPLETED, build, None),
        (ProgressEventKind.STARTED, ProgressStepId("other"), CreatePhase.PROVISIONING.step_id),
        (ProgressEventKind.SKIPPED, ProgressStepId("cached"), ProgressStepId("imbue_cloud.rebuild")),
    ]
    assert events[2].total == 4 and events[3].completed == 2
