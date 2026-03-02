from __future__ import annotations

import os
import shlex
import subprocess
import time
from io import BytesIO
from pathlib import Path
from threading import Event
from typing import Callable
from typing import Final
from typing import IO
from typing import Mapping
from typing import Self
from typing import Sequence

from loguru import logger

from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.event_utils import MutableEvent
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span

# Received a shutdown signal
SUBPROCESS_STOPPED_BY_REQUEST_EXIT_CODE: Final[int] = -9999


_READ_SIZE: Final[int] = 2**20


class FinishedProcess(FrozenModel):
    """Represents a completed process with its output and exit status."""

    returncode: int | None = None
    stdout: str
    stderr: str
    command: tuple[str, ...]
    is_timed_out: bool = False
    is_output_already_logged: bool

    def check(self) -> Self:
        from imbue.concurrency_group.errors import ProcessError

        if self.is_timed_out:
            raise ProcessTimeoutError(
                command=self.command,
                stdout=self.stdout,
                stderr=self.stderr,
                is_output_already_logged=self.is_output_already_logged,
            )
        if self.returncode != 0:
            raise ProcessError(
                command=self.command,
                returncode=self.returncode,
                stdout=self.stdout,
                stderr=self.stderr,
                is_output_already_logged=self.is_output_already_logged,
            )
        return self


class PartialOutputContainer:
    """A helper class to make reconstructing log lines returned by pipe.read() easier."""

    def __init__(
        self,
        on_complete_line: Callable[[str], None] | None = None,
    ) -> None:
        self.buffer: BytesIO = BytesIO()
        self.in_progress_line: bytearray = bytearray()
        self.on_complete_line = on_complete_line

    def write(self, output: bytes) -> None:
        """Process output which may contain newlines."""
        self.buffer.write(output)
        on_complete_line = self.on_complete_line
        if on_complete_line is None:
            return

        lines = output.splitlines(keepends=True)
        for line in lines:
            self.in_progress_line.extend(line)
            if line.endswith((b"\n", b"\r")):
                on_complete_line(self.in_progress_line.decode("utf-8", errors="replace"))
                self.in_progress_line.clear()

    def get_complete_output(self) -> bytes:
        return self.buffer.getvalue()


class OutputGatherer:
    """Gathers output from stdout and stderr of a subprocess."""

    def __init__(
        self,
        stdout: IO[bytes],
        stderr: IO[bytes],
        stdout_container: PartialOutputContainer,
        stderr_container: PartialOutputContainer,
        shutdown_event: ReadOnlyEvent,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_container = stdout_container
        self.stderr_container = stderr_container
        self.shutdown_event = shutdown_event

    @classmethod
    def build_from_popen(
        cls,
        popen: subprocess.Popen[bytes],
        on_complete_line_from_stdout: Callable[[str], None] | None,
        on_complete_line_from_stderr: Callable[[str], None] | None,
        shutdown_event: ReadOnlyEvent,
    ) -> Self:
        stdout = popen.stdout
        stderr = popen.stderr
        assert stdout is not None
        assert stderr is not None
        os.set_blocking(stdout.fileno(), False)
        os.set_blocking(stderr.fileno(), False)

        return cls(
            stdout=stdout,
            stderr=stderr,
            stdout_container=PartialOutputContainer(on_complete_line=on_complete_line_from_stdout),
            stderr_container=PartialOutputContainer(on_complete_line=on_complete_line_from_stderr),
            shutdown_event=shutdown_event,
        )

    def gather_output(self) -> None:
        is_more_from_stdout = True
        is_more_from_stderr = True
        while not self.shutdown_event.is_set() and (is_more_from_stdout or is_more_from_stderr):
            partial_stdout = self.stdout.read(_READ_SIZE)
            if partial_stdout is not None:
                self.stdout_container.write(partial_stdout)
                is_more_from_stdout = len(partial_stdout) == _READ_SIZE
            else:
                is_more_from_stdout = False
            partial_stderr = self.stderr.read(_READ_SIZE)
            if partial_stderr is not None:
                self.stderr_container.write(partial_stderr)
                is_more_from_stderr = len(partial_stderr) == _READ_SIZE
            else:
                is_more_from_stderr = False

    def get_output(self) -> tuple[bytes, bytes]:
        return self.stdout_container.get_complete_output(), self.stderr_container.get_complete_output()

    def get_incomplete_lines(self) -> tuple[str, str]:
        return self.stdout_container.in_progress_line.decode(
            "utf-8", errors="replace"
        ), self.stderr_container.in_progress_line.decode("utf-8", errors="replace")


def _shutdown_popen(process: subprocess.Popen[bytes], command: str, shutdown_timeout_sec: float) -> int | None:
    with log_span(
        "Aborting command (via sigterm to {}) due to signal...",
        process.pid,
    ):
        process.terminate()
        try:
            process.wait(timeout=shutdown_timeout_sec)
            return process.returncode
        except subprocess.TimeoutExpired:
            logger.warning("Process didn't die within {} seconds of SIGTERM", shutdown_timeout_sec)
            process.kill()
            try:
                process.wait(timeout=2)
                return process.returncode
            except subprocess.TimeoutExpired:
                logger.error("Process didn't die after kill()")
                return None


def _is_timeout(timeout_time: float | None = None) -> bool:
    if timeout_time is None:
        return False
    else:
        return time.time() > timeout_time


def run_local_command_modern_version(
    command: Sequence[str],
    is_checked: bool = True,
    timeout: float | None = None,
    trace_output: bool = False,
    cwd: Path | None = None,
    trace_on_line_callback: Callable[[str, bool], None] | None = None,
    shutdown_event: MutableEvent | None = None,
    shutdown_timeout_sec: float = 30.0,
    poll_time: float = 0.01,
    env: Mapping[str, str] | None = None,
    on_initialization_complete: Callable[[BaseException | None], None] = lambda success: None,
) -> FinishedProcess:
    """
    Run a subprocess command and return the result.

    This function handles reading stdout/stderr in real-time while monitoring for shutdown events.
    """
    try:
        shutdown_event = shutdown_event or Event()
        command_as_string = " ".join(shlex.quote(arg) for arg in command)

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                bufsize=0,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env if env is not None else os.environ.copy(),
            )
        except (OSError, ValueError) as e:
            raise ProcessSetupError(
                command=tuple(command),
                stdout="",
                stderr="",
                is_output_already_logged=trace_output,
            ) from e

        on_initialization_complete(None)
    except BaseException as e:
        on_initialization_complete(e)
        raise

    if trace_output:
        assert trace_on_line_callback, "Must pass trace_on_line_callback"
        on_complete_line_from_stdout: Callable[[str], None] | None = lambda line: trace_on_line_callback(line, True)
        on_complete_line_from_stderr: Callable[[str], None] | None = lambda line: trace_on_line_callback(line, False)
    else:
        on_complete_line_from_stdout = None
        on_complete_line_from_stderr = None

    gatherer = OutputGatherer.build_from_popen(
        process,
        on_complete_line_from_stdout=on_complete_line_from_stdout,
        on_complete_line_from_stderr=on_complete_line_from_stderr,
        shutdown_event=shutdown_event,
    )

    timeout_time = time.time() + timeout if timeout is not None else None

    while not shutdown_event.wait(poll_time) and not _is_timeout(timeout_time):
        maybe_exit_code = process.poll()
        gatherer.gather_output()
        if maybe_exit_code is not None:
            exit_code = maybe_exit_code
            break
    else:
        exit_code = _shutdown_popen(process, command_as_string, shutdown_timeout_sec)

    stdout, stderr = gatherer.get_output()

    # Send the final incomplete lines as well
    incomplete_stdout_line, incomplete_stderr_line = gatherer.get_incomplete_lines()
    if incomplete_stdout_line:
        if trace_on_line_callback:
            trace_on_line_callback(incomplete_stdout_line, True)
    if incomplete_stderr_line:
        if trace_on_line_callback:
            trace_on_line_callback(incomplete_stderr_line, False)

    result = FinishedProcess(
        returncode=exit_code,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        command=tuple(command),
        is_timed_out=_is_timeout(timeout_time),
        is_output_already_logged=trace_output,
    )
    if is_checked:
        result.check()

    return result
