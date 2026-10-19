"""Turns BuildKit's plain-mode build output into progress on one build step and its stages.

BuildKit prints one ``#<vertex> [<build stage> <k>/<n>] <instruction>`` line when
a Dockerfile stage starts and ``#<vertex> DONE <seconds>`` or ``#<vertex> CACHED``
when it finishes; the ``k/n`` pair is the same on every platform and builder
that speaks BuildKit (``docker build`` under ``BUILDKIT_PROGRESS=plain`` and
``depot build`` alike), which is why this parser keys off it and nothing else.
Each build stage (one ``FROM``) numbers its own stages from 1 with its own
``n``, so a multi-stage Dockerfile is counted per build stage and summed.

Every stage is also reported as a step of its own under the build step, named
by its instruction (:func:`~imbue.mngr.utils.progress.text_step_id`), so a
consumer can learn how long each stage takes rather than treating all stages
as equal: in a typical Dockerfile a handful of ``RUN`` stages are nearly all of
the build. A cached stage is reported skipped, not completed, so it teaches no
duration. Two of BuildKit's internal vertices are stages in all but name and
are reported the same way: loading the build context, and exporting the image,
which on a large image is a sizable share of the build and runs after the last
numbered stage.
"""

import re
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.progress import ProgressReporterInterface
from imbue.mngr.utils.progress import summarize_command_for_label
from imbue.mngr.utils.progress import text_step_id

# BuildKit right-aligns k to the width of n (``[ 5/36]``), so a space may precede it.
_STAGE_START_RE: Final[re.Pattern[str]] = re.compile(
    r"^#(?P<vertex>\d+) \[(?:(?P<build_stage>[^\]\s]+) )? *(?P<k>\d+)/(?P<n>\d+)\] "
)
_VERTEX_FINISHED_RE: Final[re.Pattern[str]] = re.compile(r"^#(?P<vertex>\d+) (?P<outcome>DONE|CACHED)\b")
_CONTEXT_START_RE: Final[re.Pattern[str]] = re.compile(r"^#(?P<vertex>\d+) \[internal\] load build context$")
_EXPORT_START_RE: Final[re.Pattern[str]] = re.compile(r"^#(?P<vertex>\d+) exporting to image$")

EXPORT_STEP_SUFFIX: Final[str] = "export"
CONTEXT_STEP_SUFFIX: Final[str] = "context"
_STAGE_LABEL_MAX_LENGTH: Final[int] = 50


class DockerBuildStageEvent(FrozenModel):
    """What one build output line said about stage progress, if anything."""

    completed_stages: int = Field(description="Stages known to be finished after this line, over every build stage")
    total_stages: int = Field(description="Stages the build has in all, over every build stage seen so far")


class _StageStep(FrozenModel):
    """The step one BuildKit vertex is reported as."""

    step_id: ProgressStepId = Field(description="The step's id under the build step")
    label: str = Field(description="The step's caption")


class DockerBuildProgressParser(MutableModel):
    """Folds build output lines into a stage count and per-stage steps, reporting each through the reporter."""

    reporter: ProgressReporterInterface = Field(frozen=True, description="Where stage progress is reported")
    step_id: ProgressStepId = Field(frozen=True, description="The build step the stages belong to")
    _total_by_build_stage: dict[str, int] = PrivateAttr(default_factory=dict)
    _finished_by_build_stage: dict[str, set[int]] = PrivateAttr(default_factory=dict)
    _stage_by_vertex: dict[int, tuple[str, int]] = PrivateAttr(default_factory=dict)
    _last_reported: DockerBuildStageEvent | None = PrivateAttr(default=None)
    _step_by_vertex: dict[int, _StageStep] = PrivateAttr(default_factory=dict)
    _finished_vertices: set[int] = PrivateAttr(default_factory=set)
    _uses_by_step_id: dict[ProgressStepId, int] = PrivateAttr(default_factory=dict)

    def feed_line(self, line: str) -> None:
        stripped = line.strip()
        self._report_stage_step(stripped)
        event = self._fold_line(stripped)
        if event is None or event == self._last_reported:
            return
        self._last_reported = event
        self.reporter.advance_step(self.step_id, event.completed_stages, event.total_stages)

    def _report_stage_step(self, line: str) -> None:
        started = _STAGE_START_RE.match(line)
        if started is not None:
            instruction = line[started.end() :]
            k, n = started.group("k"), started.group("n")
            label = f"Building stage {k}/{n}: {summarize_command_for_label(instruction, _STAGE_LABEL_MAX_LENGTH)}"
            vertex = int(started.group("vertex"))
            if vertex not in self._step_by_vertex:
                self._start_vertex(vertex, self._unique_step_id(text_step_id(self.step_id, instruction)), label)
            return
        context = _CONTEXT_START_RE.match(line)
        if context is not None:
            step_id = ProgressStepId(f"{self.step_id}.{CONTEXT_STEP_SUFFIX}")
            self._start_vertex(int(context.group("vertex")), step_id, "Loading the build context")
            return
        export = _EXPORT_START_RE.match(line)
        if export is not None:
            step_id = ProgressStepId(f"{self.step_id}.{EXPORT_STEP_SUFFIX}")
            self._start_vertex(int(export.group("vertex")), step_id, "Exporting the image")
            return
        finished = _VERTEX_FINISHED_RE.match(line)
        if finished is None:
            return
        vertex = int(finished.group("vertex"))
        step = self._step_by_vertex.get(vertex)
        if step is None or vertex in self._finished_vertices:
            return
        self._finished_vertices.add(vertex)
        if finished.group("outcome") == "CACHED":
            self.reporter.skip_step(step.step_id, step.label, self.step_id)
        else:
            self.reporter.complete_step(step.step_id)

    def _start_vertex(self, vertex: int, step_id: ProgressStepId, label: str) -> None:
        # BuildKit reprints a long-running vertex's header before its DONE line.
        if vertex in self._step_by_vertex:
            return
        self._step_by_vertex[vertex] = _StageStep(step_id=step_id, label=label)
        self.reporter.start_step(step_id, label, self.step_id, None)

    def _unique_step_id(self, step_id: ProgressStepId) -> ProgressStepId:
        """``step_id``, or a numbered variant when the same instruction already appeared in this build (in any build stage)."""
        uses = self._uses_by_step_id.get(step_id, 0) + 1
        self._uses_by_step_id[step_id] = uses
        return step_id if uses == 1 else ProgressStepId(f"{step_id}_{uses}")

    def _fold_line(self, line: str) -> DockerBuildStageEvent | None:
        started = _STAGE_START_RE.match(line)
        if started is not None:
            build_stage = started.group("build_stage") or ""
            stage = int(started.group("k"))
            self._total_by_build_stage[build_stage] = int(started.group("n"))
            self._stage_by_vertex[int(started.group("vertex"))] = (build_stage, stage)
            # Every stage before the one that just started is behind us, even when
            # BuildKit never printed a DONE line for it (parallel stages interleave).
            self._finished_by_build_stage.setdefault(build_stage, set()).update(range(1, stage))
            return self._stage_event()
        finished = _VERTEX_FINISHED_RE.match(line)
        if finished is not None:
            located = self._stage_by_vertex.get(int(finished.group("vertex")))
            if located is None:
                return None
            build_stage, stage = located
            self._finished_by_build_stage.setdefault(build_stage, set()).add(stage)
            return self._stage_event()
        return None

    def _stage_event(self) -> DockerBuildStageEvent | None:
        if not self._total_by_build_stage:
            return None
        return DockerBuildStageEvent(
            completed_stages=sum(
                min(len(self._finished_by_build_stage.get(build_stage, set())), total)
                for build_stage, total in self._total_by_build_stage.items()
            ),
            total_stages=sum(self._total_by_build_stage.values()),
        )


@pure
def count_dockerfile_instructions(dockerfile_contents: str) -> int:
    """How many build stages a Dockerfile has: one per instruction, continuation lines joined, comments skipped."""
    count = 0
    is_continuing = False
    for raw_line in dockerfile_contents.splitlines():
        line = raw_line.strip()
        if is_continuing:
            is_continuing = line.endswith("\\")
            continue
        if not line or line.startswith("#"):
            continue
        count += 1
        is_continuing = line.endswith("\\")
    return count
