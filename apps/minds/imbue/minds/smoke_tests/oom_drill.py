"""default-workspace-template's OOM drill, run inside the journey's Imbue Cloud workspace.

The template's drill (``system/services/oom_priority/bin/oom_drill.py``) checks that earlyoom sheds in the predicted
order and prints one JSON verdict. The suite runs the drill the workspace holds -- the template version the
workspace was created from -- detached, and reads its verdict back. A real workspace runs earlyoom in a gVisor
container on a pool host, which no CI run of the template does.
"""

import shlex
import shutil
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.primitives import PositiveInt
from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.data_types import DwtTestOutcome
from imbue.minds.smoke_tests.data_types import DwtTestResult
from imbue.minds.smoke_tests.interfaces import WorkspaceShellInterface
from imbue.minds.smoke_tests.workspace_runner import RunnerEnd
from imbue.minds.smoke_tests.workspace_runner import fetch_workspace_directory
from imbue.minds.smoke_tests.workspace_runner import log_tail_of
from imbue.minds.smoke_tests.workspace_runner import read_exit_code_or_none
from imbue.minds.smoke_tests.workspace_runner import start_detached_runner
from imbue.minds.smoke_tests.workspace_runner import stop_detached_runner
from imbue.minds.smoke_tests.workspace_runner import wait_for_detached_runner
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_PREAMBLE

OOM_DRILL_RELATIVE_PATH: Final[PurePosixPath] = PurePosixPath("system/services/oom_priority/bin/oom_drill.py")
# Where a run's drill leaves what it printed and logged in the workspace: outside the repo and on disk, like the CI
# suites' results.
OOM_DRILL_WORKSPACE_RESULTS_ROOT: Final[PurePosixPath] = PurePosixPath("/var/tmp/studio-smoke-oom-drill")
# The drill's result is reported as a default-workspace-template test of this suite.
OOM_DRILL_SUITE_NAME: Final[str] = "oom-drill"
# The deadline falls after the drill's own timeout, so a drill that gives up by itself still prints its verdict.
OOM_DRILL_TIMEOUT_SECONDS: Final[int] = 6 * 60
OOM_DRILL_DEADLINE_SECONDS: Final[float] = 7 * 60
# What the drill's runner leaves under its results directory, beside the runner's own files.
_VERDICT_FILE_NAME: Final[str] = "verdict.json"
_DRILL_LOG_FILE_NAME: Final[str] = "drill.log"
_DRILL_EXIT_FILE_NAME: Final[str] = "drill.exit"


class _OomDrillProcess(FrozenModel):
    """A process as one of the drill's snapshots saw it."""

    model_config = ConfigDict(extra="ignore")

    pid: int = Field(description="The process's pid.")
    comm: str = Field(description="The process's name.")
    oom_score_adj: int = Field(description="Its ``oom_score_adj`` in the snapshot.")


class _OomDrillJudgement(FrozenModel):
    """How the drill judged one kill against its prediction."""

    model_config = ConfigDict(extra="ignore")

    verdict: str = Field(description="``right``, ``wrong``, or ``unpredicted`` for a victim it never saw.")
    top: _OomDrillProcess | None = Field(description="The process it predicted earlyoom would pick.")


class _OomDrillKill(FrozenModel):
    """One kill earlyoom made during the drill."""

    model_config = ConfigDict(extra="ignore")

    pid: int = Field(description="The victim's pid.")
    comm: str | None = Field(description="The victim's name, as the shed ledger recorded it.")
    band: int | None = Field(
        description="The band of the sleeper killed, or None for a process the drill did not start."
    )
    judgement: _OomDrillJudgement = Field(description="How the drill judged the kill.")


class _OomDrillVerdict(FrozenModel):
    """The JSON verdict the drill prints, as far as the suite reads it."""

    model_config = ConfigDict(extra="ignore")

    passed: bool = Field(description="Whether every sleeper was shed, each the predicted victim.")
    failure: str = Field(description="Why the drill failed; empty when it passed.")
    bands: tuple[int, ...] = Field(description="The bands the drill started one sleeper at, in its order.")
    sleeper_bands_in_shed_order: tuple[int, ...] = Field(description="The bands of the sleepers shed, in order.")
    kills: tuple[_OomDrillKill, ...] = Field(description="Every kill earlyoom made during the drill, in order.")


@pure
def build_oom_drill_runner_script(
    results_dir: PurePosixPath, repo_root: PurePosixPath, drill_timeout_seconds: int
) -> str:
    """The bash script running the template's drill from ``repo_root``, keeping what it prints, logs and exits with."""
    results = '"$results_dir"'
    return (
        "\n".join(
            (
                "#!/usr/bin/env bash",
                "# Runs default-workspace-template's OOM drill for the Imbue Studio smoke suite, which reads what it",
                "# leaves under the results directory.",
                "set -uo pipefail",
                f"readonly results_dir={shlex.quote(str(results_dir))}",
                'mkdir -p "$results_dir"',
                RUNNER_PREAMBLE,
                f"cd {shlex.quote(str(repo_root))} || exit 1",
                f"python3 {shlex.quote(str(OOM_DRILL_RELATIVE_PATH))} --timeout {drill_timeout_seconds} "
                f"> {results}/{_VERDICT_FILE_NAME} 2> {results}/{_DRILL_LOG_FILE_NAME} < /dev/null",
                f'echo "$?" > {results}/{_DRILL_EXIT_FILE_NAME}',
            )
        )
        + "\n"
    )


def run_oom_drill_in_workspace(
    workspace: WorkspaceShellInterface,
    repo_root: PurePosixPath,
    workspace_results_dir: PurePosixPath,
    local_results_dir: Path,
    drill_timeout_seconds: int,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> DwtTestResult:
    """Run the template's drill inside the workspace and bring back its result.

    The runner is started detached and polled, so the drill's memory pressure hangs off no single connection; past
    ``deadline_seconds`` it is stopped. What it left lands under ``local_results_dir``, which is emptied first so
    that an earlier run's verdict cannot be read as this run's.
    """
    if local_results_dir.exists():
        shutil.rmtree(local_results_dir)
    script = build_oom_drill_runner_script(workspace_results_dir, repo_root, drill_timeout_seconds)
    with log_span("Starting default-workspace-template's OOM drill in the workspace under {}", workspace_results_dir):
        start_detached_runner(workspace, script, workspace_results_dir, repo_root)
    runner_end = wait_for_detached_runner(
        workspace, workspace_results_dir, repo_root, deadline_seconds, poll_interval_seconds
    )
    if runner_end == RunnerEnd.OUT_OF_TIME:
        stop_detached_runner(workspace, workspace_results_dir, repo_root)
    with log_span("Fetching the OOM drill's verdict from the workspace into {}", local_results_dir):
        fetch_workspace_directory(workspace, workspace_results_dir, local_results_dir, repo_root)
    return read_oom_drill_result(local_results_dir, runner_end, deadline_seconds)


def read_oom_drill_result(results_dir: Path, runner_end: RunnerEnd, deadline_seconds: float) -> DwtTestResult:
    """The drill's result from what its runner left under ``results_dir``."""
    verdict = _read_verdict_or_none(results_dir / _VERDICT_FILE_NAME)
    exit_code = read_exit_code_or_none(results_dir / _DRILL_EXIT_FILE_NAME)
    is_passed = runner_end == RunnerEnd.FINISHED and verdict is not None and verdict.passed and exit_code == 0
    drill_log_tail = log_tail_of(results_dir / _DRILL_LOG_FILE_NAME)
    return DwtTestResult(
        suite_name=NonEmptyStr(OOM_DRILL_SUITE_NAME),
        test_id=NonEmptyStr(str(OOM_DRILL_RELATIVE_PATH)),
        outcome=DwtTestOutcome.PASSED if is_passed else DwtTestOutcome.FAILED,
        attempt_count=PositiveInt(1),
        detail="" if is_passed else _failure_detail(runner_end, deadline_seconds, verdict, exit_code, drill_log_tail),
    )


@pure
def oom_drill_markdown(result: DwtTestResult, *, template_ref: str | None) -> str:
    """A summary for a CI job's page: whether the drill passed, and why not when it did not.

    ``template_ref`` is the tag the workspace's pool row was baked at, when the journey could read it.
    """
    outcome_lines = (
        ("Passed: every sleeper was shed in band order, each the victim the drill predicted.",)
        if result.outcome == DwtTestOutcome.PASSED
        else ("Failed:", "", "```text", result.detail, "```")
    )
    lines = (
        "## default-workspace-template's OOM drill in the workspace",
        "",
        *((f"Run in a workspace on a pool row baked at template `{template_ref}`.", "") if template_ref else ()),
        *outcome_lines,
    )
    return "\n".join(lines) + "\n"


def _read_verdict_or_none(verdict_path: Path) -> _OomDrillVerdict | None:
    """The verdict the drill printed, or None when it printed none or the runner was cut off writing it."""
    if not verdict_path.is_file():
        return None
    text = verdict_path.read_text(errors="replace")
    if not text.strip():
        return None
    try:
        return _OomDrillVerdict.model_validate_json(text)
    except ValidationError as exc:
        logger.warning("Read the drill's unreadable verdict {} as none: {}", verdict_path, exc)
        return None


@pure
def _failure_detail(
    runner_end: RunnerEnd,
    deadline_seconds: float,
    verdict: _OomDrillVerdict | None,
    exit_code: int | None,
    drill_log_tail: str,
) -> str:
    """Why the drill failed."""
    match runner_end:
        case RunnerEnd.FINISHED:
            if verdict is not None:
                return _verdict_failure_detail(verdict)
            exited = f"exited {exit_code}" if exit_code is not None else "recorded no exit code"
            problem = f"The drill printed no verdict and {exited}."
        case RunnerEnd.VANISHED:
            problem = "The drill's runner ended without finishing, as a process the workspace's OOM killer sheds does."
        case RunnerEnd.OUT_OF_TIME:
            problem = (
                f"The drill did not finish within {deadline_seconds / 60:.0f} minutes, so its runner was stopped."
            )
        case _ as unreachable:
            assert_never(unreachable)
    return "\n\n".join(part for part in (problem, drill_log_tail) if part)


@pure
def _verdict_failure_detail(verdict: _OomDrillVerdict) -> str:
    """Why a drill that ran to its verdict failed."""
    return "\n".join(
        (
            f"The drill failed: {verdict.failure}",
            f"Sleepers shed, in order: {_bands_phrase(verdict.sleeper_bands_in_shed_order)} "
            f"(of {_bands_phrase(verdict.bands)})",
            "Kills, in order:",
            *(f"  {_kill_phrase(kill)}" for kill in verdict.kills),
        )
    )


@pure
def _bands_phrase(bands: tuple[int, ...]) -> str:
    return ", ".join(str(band) for band in bands) if bands else "none"


@pure
def _kill_phrase(kill: _OomDrillKill) -> str:
    victim = f"pid {kill.pid} {kill.comm}" + (f", the sleeper at {kill.band}" if kill.band is not None else "")
    top = kill.judgement.top
    if kill.judgement.verdict == "wrong" and top is not None:
        return f"{victim}: wrong, predicted pid {top.pid} {top.comm} at {top.oom_score_adj}"
    return f"{victim}: {kill.judgement.verdict}"
