"""Turns Modal's image build log into progress on the ``modal.build`` step and its instructions.

Modal streams its builder's output as it runs: ``Building image im-...``, then
``=> Step N: <instruction>`` as each Dockerfile instruction starts, ``Saving
image...`` and ``Built image im-... in Xs`` at the end. The client prints the
stream through mngr's log capture, which this parser watches; the step counter
is the only progress the stream carries, so it is all the parser keys off.
A build may chain several images (the Dockerfile's, then mngr's additions on
top of it); each one's steps are counted from the instruction count of the
Dockerfile that drove it, and a later image simply keeps advancing.

Each instruction is also reported as a step of its own under the build step,
named by its text (:func:`~imbue.mngr.utils.progress.text_step_id`), as is the
save at the end, so a consumer can learn how long each one takes. Modal prints
nothing for an image it already has, so a fully cached build reports no
instruction steps at all.
"""

import re
import threading
from collections.abc import Callable
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.primitives import ProgressStepId
from imbue.mngr.utils.progress import ProgressReporterInterface
from imbue.mngr.utils.progress import summarize_command_for_label
from imbue.mngr.utils.progress import text_step_id

MODAL_BUILD_STEP: Final[ProgressStepId] = ProgressStepId("modal.build")
SAVE_STEP_SUFFIX: Final[str] = "save"

_STEP_RE: Final[re.Pattern[str]] = re.compile(r"^=> Step (?P<n>\d+): ")
_SAVING_RE: Final[re.Pattern[str]] = re.compile(r"^Saving image\.\.\.")
_BUILT_RE: Final[re.Pattern[str]] = re.compile(r"^Built image \S+ in ")
_STEP_LABEL_MAX_LENGTH: Final[int] = 50

# How often the watcher re-reads the captured output while a build runs.
_WATCH_INTERVAL_SECONDS: Final[float] = 0.5


class ModalBuildProgressParser(MutableModel):
    """Folds Modal build log lines into a step count and per-instruction steps, reporting each through the reporter."""

    reporter: ProgressReporterInterface = Field(frozen=True, description="Where step progress is reported")
    total_steps: int | None = Field(
        frozen=True, description="Instructions in the Dockerfile that drives the build, when one is known"
    )
    _started_steps: set[int] = PrivateAttr(default_factory=set)
    _last_reported: int | None = PrivateAttr(default=None)
    _running_line: str | None = PrivateAttr(default=None)
    _running_unique_id: ProgressStepId | None = PrivateAttr(default=None)
    _uses_by_step_id: dict[ProgressStepId, int] = PrivateAttr(default_factory=dict)

    def feed_line(self, line: str) -> None:
        stripped = line.strip()
        step_match = _STEP_RE.match(stripped)
        if step_match is not None:
            instruction = stripped[step_match.end() :]
            self._run_step(
                stripped,
                text_step_id(MODAL_BUILD_STEP, instruction),
                f"Step {step_match.group('n')}: {summarize_command_for_label(instruction, _STEP_LABEL_MAX_LENGTH)}",
            )
            # Step n starting means steps 0..n-1 are behind us.
            self._started_steps.add(int(step_match.group("n")))
            self._report(max(self._started_steps))
            return
        if _SAVING_RE.match(stripped):
            self._run_step(stripped, ProgressStepId(f"{MODAL_BUILD_STEP}.{SAVE_STEP_SUFFIX}"), "Saving the image")
            return
        if _BUILT_RE.match(stripped):
            self._finish_running_step()
            if self.total_steps is not None:
                self._report(self.total_steps)

    def _run_step(self, line: str, step_id: ProgressStepId, label: str) -> None:
        """Start the step ``line`` announces, finishing the one before it.

        Modal prints every line twice, so the line that is already running is
        ignored; a second instruction with the same text is a new step, since
        its line carries a different step number.
        """
        if line == self._running_line:
            return
        self._finish_running_step()
        uses = self._uses_by_step_id.get(step_id, 0) + 1
        self._uses_by_step_id[step_id] = uses
        # A chained image repeats the save, and a Dockerfile may repeat an instruction.
        unique_id = step_id if uses == 1 else ProgressStepId(f"{step_id}_{uses}")
        self._running_line = line
        self._running_unique_id = unique_id
        self.reporter.start_step(unique_id, label, MODAL_BUILD_STEP, None)

    def _finish_running_step(self) -> None:
        if self._running_unique_id is None:
            return
        self.reporter.complete_step(self._running_unique_id)
        self._running_line = None
        self._running_unique_id = None

    def _report(self, completed: int) -> None:
        capped = min(completed, self.total_steps) if self.total_steps is not None else completed
        # A later image in the chain restarts its own count at step 0; the
        # build as a whole never moves backwards.
        if self._last_reported is not None and capped <= self._last_reported:
            return
        self._last_reported = capped
        self.reporter.advance_step(MODAL_BUILD_STEP, capped, self.total_steps)


class ModalBuildOutputWatcher(MutableModel):
    """Feeds the lines a Modal build appends to the captured output into the parser while the build runs."""

    read_captured_output: Callable[[], str] = Field(frozen=True, description="Reads everything captured so far")
    parser: ModalBuildProgressParser = Field(frozen=True, description="Where new lines go")
    start_offset: int = Field(
        frozen=True, description="Where this build's output begins in the capture; what precedes it is never parsed"
    )
    _consumed_length: int = PrivateAttr(default=0)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def feed_new_output(self) -> None:
        """Hand every complete line captured since the last call to the parser."""
        captured = self.read_captured_output()
        new_text = captured[self.start_offset + self._consumed_length :]
        last_newline = new_text.rfind("\n")
        if last_newline < 0:
            return
        for line in new_text[:last_newline].splitlines():
            self.parser.feed_line(line)
        self._consumed_length += last_newline + 1

    def run_until_stopped(self) -> None:
        """The watcher thread's body: poll the capture until stopped, then drain once more."""
        while not self._stop_event.wait(_WATCH_INTERVAL_SECONDS):
            self.feed_new_output()
        self.feed_new_output()

    def stop(self) -> None:
        self._stop_event.set()
