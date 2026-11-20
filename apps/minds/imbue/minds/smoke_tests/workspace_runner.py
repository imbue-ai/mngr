"""A script run detached inside the journey's workspace, polled until it ends, and what it left fetched back.

The script starts in a session of its own, so a long run hangs off no single connection. Short commands then
poll it through the files it keeps under its results directory, and everything under that directory comes back
as one archive.
"""

import base64
import binascii
import io
import shlex
import tarfile
import threading
import time
from enum import auto
from pathlib import Path
from pathlib import PurePosixPath
from typing import Final
from typing import assert_never

from loguru import logger
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.logging import log_span
from imbue.minds.smoke_tests.interfaces import WorkspaceShellInterface
from imbue.minds.smoke_tests.primitives import SmokeWorkspaceExecError
from imbue.minds.smoke_tests.workspace_runner_scripts import DETACH_PROGRAM
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_DONE_FILE_NAME
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_LOG_FILE_NAME
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_PID_FILE_NAME
from imbue.minds.smoke_tests.workspace_runner_scripts import RUNNER_STATE_COMMAND_TEMPLATE

SHORT_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
# Enough of a log to show why a run went wrong without burying the report.
_LOG_TAIL_LINE_COUNT: Final[int] = 40
_RUNNER_SCRIPT_FILE_NAME: Final[str] = "run.sh"
_RUNNER_STATE_READ_ATTEMPT_COUNT: Final[int] = 4
_FETCH_TIMEOUT_SECONDS: Final[float] = 600.0


class RunnerEnd(UpperCaseStrEnum):
    """How the wait for a runner in the workspace ended."""

    FINISHED = auto()
    VANISHED = auto()
    OUT_OF_TIME = auto()


class _RunnerState(UpperCaseStrEnum):
    """What one look at a runner in the workspace found."""

    STARTING = auto()
    RUNNING = auto()
    FINISHED = auto()
    VANISHED = auto()


def start_detached_runner(
    workspace: WorkspaceShellInterface, script: str, workspace_results_dir: PurePosixPath, cwd: PurePosixPath
) -> None:
    """Write the runner into a fresh results directory and start it in a session of its own."""
    script_path = workspace_results_dir / _RUNNER_SCRIPT_FILE_NAME
    encoded_script = base64.b64encode(script.encode()).decode()
    command = " && ".join(
        (
            f"rm -rf {shlex.quote(str(workspace_results_dir))}",
            f"mkdir -p {shlex.quote(str(workspace_results_dir))}",
            f"printf '%s' {shlex.quote(encoded_script)} | base64 -d > {shlex.quote(str(script_path))}",
            " ".join(
                (
                    "python3 -c",
                    shlex.quote(DETACH_PROGRAM),
                    shlex.quote(str(script_path)),
                    shlex.quote(str(workspace_results_dir / RUNNER_LOG_FILE_NAME)),
                )
            ),
        )
    )
    workspace.run(command, cwd, SHORT_COMMAND_TIMEOUT_SECONDS)


def wait_for_detached_runner(
    workspace: WorkspaceShellInterface,
    workspace_results_dir: PurePosixPath,
    cwd: PurePosixPath,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> RunnerEnd:
    """Poll the runner until it finishes, vanishes, or outlives the deadline, logging each step it reaches."""
    deadline = time.monotonic() + deadline_seconds
    last_progress = ""
    while time.monotonic() < deadline:
        threading.Event().wait(timeout=poll_interval_seconds)
        state, progress = _read_runner_state(workspace, workspace_results_dir, cwd)
        if progress and progress != last_progress:
            logger.info("The runner in the workspace is on: {}", progress)
            last_progress = progress
        match state:
            case _RunnerState.FINISHED:
                return RunnerEnd.FINISHED
            case _RunnerState.VANISHED:
                return RunnerEnd.VANISHED
            case _RunnerState.STARTING | _RunnerState.RUNNING:
                pass
            case _ as unreachable:
                assert_never(unreachable)
    return RunnerEnd.OUT_OF_TIME


@retry(
    retry=retry_if_exception_type(SmokeWorkspaceExecError),
    stop=stop_after_attempt(_RUNNER_STATE_READ_ATTEMPT_COUNT),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _read_runner_state(
    workspace: WorkspaceShellInterface, workspace_results_dir: PurePosixPath, cwd: PurePosixPath
) -> tuple[_RunnerState, str]:
    """The runner's state, and the last line it logged; a dropped connection is retried before it counts."""
    stdout = workspace.run(
        RUNNER_STATE_COMMAND_TEMPLATE.format(
            done=shlex.quote(str(workspace_results_dir / RUNNER_DONE_FILE_NAME)),
            pid=shlex.quote(str(workspace_results_dir / RUNNER_PID_FILE_NAME)),
            log=shlex.quote(str(workspace_results_dir / RUNNER_LOG_FILE_NAME)),
        ),
        cwd,
        SHORT_COMMAND_TIMEOUT_SECONDS,
    )
    state_line, _, progress = stdout.partition("\n")
    try:
        state = _RunnerState(state_line.strip())
    except ValueError as exc:
        raise SmokeWorkspaceExecError(f"The runner's state read as {stdout[:200]!r}, not one of its states.") from exc
    return state, progress.strip()


def stop_detached_runner(
    workspace: WorkspaceShellInterface, workspace_results_dir: PurePosixPath, cwd: PurePosixPath
) -> None:
    """End the runner's whole session, whatever it is waiting on included."""
    pid_path = shlex.quote(str(workspace_results_dir / RUNNER_PID_FILE_NAME))
    with log_span("Stopping the runner in the workspace, which outlived its deadline"):
        workspace.run(f'kill -TERM -- -"$(cat {pid_path})" 2>/dev/null; true', cwd, SHORT_COMMAND_TIMEOUT_SECONDS)


def fetch_workspace_directory(
    workspace: WorkspaceShellInterface, workspace_dir: PurePosixPath, local_dir: Path, cwd: PurePosixPath
) -> None:
    """Copy everything under ``workspace_dir`` into ``local_dir``, as a gzipped tarball over the command's stdout."""
    encoded_archive = workspace.run(
        f"tar -C {shlex.quote(str(workspace_dir))} -czf - . | base64", cwd, _FETCH_TIMEOUT_SECONDS
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded_archive)), mode="r:gz") as archive:
            archive.extractall(local_dir, filter="data")
    except (binascii.Error, tarfile.TarError, EOFError) as exc:
        raise SmokeWorkspaceExecError(f"The workspace sent an unreadable archive of {workspace_dir}: {exc}") from exc


def read_exit_code_or_none(exit_path: Path) -> int | None:
    """The exit code a runner recorded, or None when it never recorded one."""
    if not exit_path.is_file():
        return None
    text = exit_path.read_text().strip()
    if not text.isdigit():
        logger.warning("Read the exit file {} holding {!r} as no exit code", exit_path, text)
        return None
    return int(text)


def log_tail_of(log_path: Path) -> str:
    """The last lines of a log a runner left, or nothing when it left none."""
    if not log_path.is_file():
        return ""
    return "\n".join(log_path.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINE_COUNT:])
