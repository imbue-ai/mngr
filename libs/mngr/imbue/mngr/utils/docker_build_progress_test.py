from inline_snapshot import snapshot

from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.docker_build_progress import DockerBuildProgressParser
from imbue.mngr.utils.docker_build_progress import count_dockerfile_instructions
from imbue.mngr.utils.progress import ProgressEvent
from imbue.mngr.utils.progress import ProgressEventKind
from imbue.mngr.utils.progress import ProgressEventSinkReporter

_BUILD_STEP = ProgressStepId("docker.build")

# A BuildKit plain-mode stream as `docker build` prints it: internal vertices,
# a multi-stage build (the stage name inside the brackets), cached and built
# stages, and the export step after the last stage.
_BUILD_OUTPUT = """
#0 building with "default" instance using docker driver
#1 [internal] load build definition from Dockerfile
#1 transferring dockerfile: 2.10kB done
#1 DONE 0.0s
#2 [internal] load metadata for docker.io/library/python:3.12-slim
#2 DONE 0.4s
#3 [stage-1 1/4] FROM docker.io/library/python:3.12-slim@sha256:abc
#3 resolve docker.io/library/python:3.12-slim@sha256:abc done
#3 DONE 0.0s
#4 [internal] load build context
#4 transferring context: 1.52MB 0.1s done
#4 DONE 0.1s
#5 [stage-1 2/4] RUN apt-get update && apt-get install -y git
#5 CACHED
#6 [stage-1 3/4] COPY . /app
#6 DONE 0.2s
#7 [stage-1 4/4] RUN pip install -e /app
#7 3.012 Installing collected packages
#7 [stage-1 4/4] RUN pip install -e /app
#7 DONE 3.4s
#8 exporting to image
#8 exporting layers done
#8 writing image sha256:def done
#8 naming to docker.io/library/mngr-build-host-1 done
#8 DONE 0.1s
"""


def _events(output: str) -> list[ProgressEvent]:
    events: list[ProgressEvent] = []
    parser = DockerBuildProgressParser(reporter=ProgressEventSinkReporter(emit=events.append), step_id=_BUILD_STEP)
    for line in output.splitlines():
        parser.feed_line(line)
    return events


def _advances(output: str) -> list[tuple[int | None, int | None]]:
    return [(event.completed, event.total) for event in _events(output) if event.kind is ProgressEventKind.ADVANCED]


def _stage_steps(output: str) -> list[tuple[str, str, str | None]]:
    """(kind, step id, label) of every event about a stage step, in order."""
    return [
        (event.kind.value.lower(), str(event.step_id), event.label)
        for event in _events(output)
        if event.kind is not ProgressEventKind.ADVANCED
    ]


def test_parser_counts_stages_as_they_start_and_finish() -> None:
    assert _advances(_BUILD_OUTPUT) == snapshot([(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)])


def test_parser_reports_each_stage_as_a_step_named_by_its_instruction_and_skips_cached_ones() -> None:
    assert _stage_steps(_BUILD_OUTPUT) == snapshot(
        [
            (
                "started",
                "docker.build.from_docker_io_9ac02b",
                "Building stage 1/4: FROM docker.io/library/python:3.12-slim@sha256:abc",
            ),
            ("completed", "docker.build.from_docker_io_9ac02b", None),
            ("started", "docker.build.context", "Loading the build context"),
            ("completed", "docker.build.context", None),
            (
                "started",
                "docker.build.run_apt_get_17f717",
                "Building stage 2/4: RUN apt-get update && apt-get install -y git",
            ),
            (
                "skipped",
                "docker.build.run_apt_get_17f717",
                "Building stage 2/4: RUN apt-get update && apt-get install -y git",
            ),
            ("started", "docker.build.copy_app_184a14", "Building stage 3/4: COPY . /app"),
            ("completed", "docker.build.copy_app_184a14", None),
            ("started", "docker.build.run_pip_install_877215", "Building stage 4/4: RUN pip install -e /app"),
            ("completed", "docker.build.run_pip_install_877215", None),
            ("started", "docker.build.export", "Exporting the image"),
            ("completed", "docker.build.export", None),
        ]
    )
    # Every stage step nests under the build step.
    assert all(
        event.parent_id == _BUILD_STEP
        for event in _events(_BUILD_OUTPUT)
        if event.kind in (ProgressEventKind.STARTED, ProgressEventKind.SKIPPED)
    )


def test_parser_reports_each_count_once_even_when_lines_repeat() -> None:
    repeated = "\n".join(line for line in _BUILD_OUTPUT.splitlines() for _ in range(2))

    assert _advances(repeated) == _advances(_BUILD_OUTPUT)
    assert _stage_steps(repeated) == _stage_steps(_BUILD_OUTPUT)


def test_parser_keeps_a_stage_id_when_stages_are_added_around_it_and_numbers_a_repeated_instruction() -> None:
    before = "#4 [1/2] FROM debian\n#4 DONE 0.0s\n#5 [2/2] RUN make\n#5 DONE 1.0s\n"
    after = "#4 [1/3] FROM debian\n#4 DONE 0.0s\n#5 [2/3] RUN prep\n#5 DONE 1.0s\n#6 [3/3] RUN make\n#6 DONE 1.0s\n"
    repeated = "#4 [1/3] FROM debian\n#4 DONE 0.0s\n#5 [2/3] RUN make\n#5 DONE 1.0s\n#6 [3/3] RUN make\n#6 DONE 1.0s\n"

    ids_before = {step_id for _, step_id, _ in _stage_steps(before)}
    ids_after = {step_id for _, step_id, _ in _stage_steps(after)}
    assert ids_before <= ids_after
    make_ids = [step_id for kind, step_id, _ in _stage_steps(repeated) if kind == "started" and "run_make" in step_id]
    assert len(make_ids) == 2 and make_ids[1] == make_ids[0] + "_2"
    # A reprinted header (BuildKit repeats a long vertex's) is not a repeat of the instruction.
    reprinted = repeated.replace("#5 [2/3] RUN make\n", "#5 [2/3] RUN make\n#5 1.000 building\n#5 [2/3] RUN make\n")
    assert [step_id for kind, step_id, _ in _stage_steps(reprinted) if kind == "started"] == [
        step_id for kind, step_id, _ in _stage_steps(repeated) if kind == "started"
    ]


def test_parser_handles_single_stage_builds_without_a_stage_name() -> None:
    output = """
#4 [1/2] FROM docker.io/library/debian:bookworm-slim
#4 DONE 0.0s
#5 [2/2] RUN echo hi
#5 DONE 0.3s
"""

    assert _advances(output) == [(0, 2), (1, 2), (2, 2)]


def test_parser_reads_the_padded_stage_numbers_of_a_build_with_ten_or_more_stages() -> None:
    output = "#5 [ 1/12] FROM debian\n#5 DONE 0.0s\n#6 [ 2/12] RUN a\n#6 DONE 1.0s\n#7 [12/12] RUN z\n#7 DONE 1.0s\n"

    assert _advances(output) == [(0, 12), (1, 12), (2, 12), (11, 12), (12, 12)]
    assert [label for kind, _, label in _stage_steps(output) if kind == "started"] == [
        "Building stage 1/12: FROM debian",
        "Building stage 2/12: RUN a",
        "Building stage 12/12: RUN z",
    ]


def test_parser_counts_each_build_stage_of_a_multi_stage_dockerfile_and_never_goes_backwards() -> None:
    # Two build stages interleave; each numbers its own stages from 1 with its own n.
    output = """
#3 [builder 1/2] FROM docker.io/library/golang:1.22
#3 DONE 0.0s
#4 [stage-1 1/3] FROM docker.io/library/debian:bookworm-slim
#4 DONE 0.0s
#5 [builder 2/2] RUN go build -o /out/app
#5 DONE 4.0s
#6 [stage-1 2/3] COPY --from=builder /out/app /usr/local/bin/app
#6 DONE 0.1s
#7 [stage-1 3/3] RUN chmod +x /usr/local/bin/app
#7 DONE 0.1s
"""

    advances = _advances(output)

    assert advances == snapshot([(0, 2), (1, 2), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5)])
    completed = [done for done, _ in advances]
    assert completed == sorted(completed)


def test_parser_ignores_output_with_no_stage_markers() -> None:
    assert _events("Sending build context to Docker daemon\nStep 1/3 : FROM debian\n") == []


def test_count_dockerfile_instructions_joins_continuations_and_skips_comments() -> None:
    dockerfile = """
# base
FROM python:3.12-slim

RUN apt-get update && \\
    apt-get install -y git \\
    && rm -rf /var/lib/apt/lists/*
COPY . /app
# trailing comment
"""

    assert count_dockerfile_instructions(dockerfile) == 3
    assert count_dockerfile_instructions("") == 0
