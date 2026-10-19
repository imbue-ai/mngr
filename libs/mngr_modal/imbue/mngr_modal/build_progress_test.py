from inline_snapshot import snapshot

from imbue.mngr.utils.progress import ProgressEvent
from imbue.mngr.utils.progress import ProgressEventKind
from imbue.mngr.utils.progress import ProgressEventSinkReporter
from imbue.mngr_modal.build_progress import MODAL_BUILD_STEP
from imbue.mngr_modal.build_progress import ModalBuildOutputWatcher
from imbue.mngr_modal.build_progress import ModalBuildProgressParser

# Recorded from a real `modal.Image.from_dockerfile(...).build(app)` of a
# four-instruction Dockerfile (a FROM and three RUNs): Modal prints each line
# twice (its console and the captured stream), builds the Dockerfile image,
# then a second image on top.
_RECORDED_BUILD_LOG = """\
Building image im-aMbeUxEeICfvpelxQMr4iy
Building image im-aMbeUxEeICfvpelxQMr4iy

=> Step 0: FROM python:3.12-slim

=> Step 0: FROM python:3.12-slim
Getting image source signatures
Copying blob sha256:bbc5a12237474ec0b23b5051260d56d6436a24d70d9cad961adebe7a037ed813
Writing manifest to image destination
Unpacking OCI image
   • unpacking rootfs ...
   • ... done
Copied image in 1.13s

=> Step 1: RUN echo "probe step one 48213" > /tmp/one
Copied image in 1.13s

=> Step 1: RUN echo "probe step one 48213" > /tmp/one

=> Step 2: RUN echo "probe step two 48213" > /tmp/two

=> Step 2: RUN echo "probe step two 48213" > /tmp/two

=> Step 3: RUN sleep 2 && echo "probe step three 48213" > /tmp/three

=> Step 3: RUN sleep 2 && echo "probe step three 48213" > /tmp/three
Saving image...
Saving image...
Image saved, took 1.31s
Image saved, took 1.31s

Built image im-aMbeUxEeICfvpelxQMr4iy in 9.63s

Built image im-aMbeUxEeICfvpelxQMr4iy in 9.63s
Building image im-HBltYw2jHmBYesSM8EIMen

=> Step 0: FROM base
Saving image...
Image saved, took 1.10s

Built image im-HBltYw2jHmBYesSM8EIMen in 2.12s
"""


def _events(log: str, total_steps: int | None) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    parser = ModalBuildProgressParser(reporter=ProgressEventSinkReporter(emit=events.append), total_steps=total_steps)
    for line in log.splitlines():
        parser.feed_line(line)
    return events


def _advances(log: str, total_steps: int | None) -> list[tuple[int | None, int | None]]:
    advances = [event for event in _events(log, total_steps) if event.kind is ProgressEventKind.ADVANCED]
    assert all(event.step_id == MODAL_BUILD_STEP for event in advances)
    return [(event.completed, event.total) for event in advances]


def test_parser_counts_recorded_modal_steps_once_each_and_finishes_on_built() -> None:
    assert _advances(_RECORDED_BUILD_LOG, total_steps=4) == snapshot([(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)])


def test_parser_without_a_known_total_reports_steps_seen_and_never_a_total() -> None:
    assert _advances(_RECORDED_BUILD_LOG, total_steps=None) == snapshot([(0, None), (1, None), (2, None), (3, None)])


def test_parser_reports_each_instruction_and_the_save_as_steps_under_the_build_once_each() -> None:
    steps = [
        (event.kind.value.lower(), str(event.step_id), event.label)
        for event in _events(_RECORDED_BUILD_LOG, total_steps=4)
        if event.kind is not ProgressEventKind.ADVANCED
    ]

    # Modal prints every line twice, so each instruction starts once; the
    # chained image's own FROM and save are steps of their own.
    assert steps == snapshot(
        [
            ("started", "modal.build.from_python_3_1b8255", "Step 0: FROM python:3.12-slim"),
            ("completed", "modal.build.from_python_3_1b8255", None),
            ("started", "modal.build.run_echo_probe_188091", 'Step 1: RUN echo "probe step one 48213" > /tmp/one'),
            ("completed", "modal.build.run_echo_probe_188091", None),
            ("started", "modal.build.run_echo_probe_65411c", 'Step 2: RUN echo "probe step two 48213" > /tmp/two'),
            ("completed", "modal.build.run_echo_probe_65411c", None),
            ("started", "modal.build.run_sleep_2_4b77ef", 'Step 3: RUN sleep 2 && echo "probe step three 48213" >...'),
            ("completed", "modal.build.run_sleep_2_4b77ef", None),
            ("started", "modal.build.save", "Saving the image"),
            ("completed", "modal.build.save", None),
            ("started", "modal.build.from_base_7ca77c", "Step 0: FROM base"),
            ("completed", "modal.build.from_base_7ca77c", None),
            ("started", "modal.build.save_2", "Saving the image"),
            ("completed", "modal.build.save_2", None),
        ]
    )
    assert all(
        event.parent_id == MODAL_BUILD_STEP
        for event in _events(_RECORDED_BUILD_LOG, total_steps=4)
        if event.kind is ProgressEventKind.STARTED
    )


def test_parser_tells_a_repeated_instruction_from_modals_duplicate_printing() -> None:
    log = "=> Step 0: FROM base\n=> Step 0: FROM base\n=> Step 1: RUN x\n=> Step 1: RUN x\n=> Step 2: RUN x\n"

    started = [str(event.step_id) for event in _events(log, None) if event.kind is ProgressEventKind.STARTED]

    assert len(started) == 3
    assert started[2] == started[1] + "_2"


def test_parser_never_reports_past_the_known_total() -> None:
    assert _advances(_RECORDED_BUILD_LOG, total_steps=2) == [(0, 2), (1, 2), (2, 2)]


def test_watcher_feeds_only_complete_new_lines_of_its_own_build_each_time() -> None:
    earlier_build = "=> Step 5: RUN earlier\nBuilt image im-0 in 1.00s\n"
    captured = {"text": earlier_build}
    events: list[ProgressEvent] = []
    watcher = ModalBuildOutputWatcher(
        read_captured_output=lambda: captured["text"],
        parser=ModalBuildProgressParser(reporter=ProgressEventSinkReporter(emit=events.append), total_steps=3),
        start_offset=len(earlier_build),
    )

    watcher.feed_new_output()
    assert events == []

    captured["text"] += "Building image im-1\n=> Step 0: FROM base\n=> Step 1: RUN a"
    watcher.feed_new_output()
    assert _completed_counts(events) == [0]

    captured["text"] += "\n=> Step 2: RUN b\n"
    watcher.feed_new_output()
    assert _completed_counts(events) == [0, 1, 2]

    watcher.feed_new_output()
    assert _completed_counts(events) == [0, 1, 2]


def _completed_counts(events: list[ProgressEvent]) -> list[int | None]:
    return [event.completed for event in events if event.kind is ProgressEventKind.ADVANCED]
