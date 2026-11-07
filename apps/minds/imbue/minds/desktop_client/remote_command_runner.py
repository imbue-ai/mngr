"""Run one command on this computer for a workspace agent, streaming what it prints.

The executor behind ``POST /api/v1/remote-execution/<device_id>``. It is kept
apart from the route so it can be exercised with a real subprocess: the route
only authenticates, checks the device and the master switch, and hands the
validated request here.

The stream is the owner-exec ``/run`` shape (``libs/imbue_common/.../owner_exec_client.py``)
minus its signature trailer: one JSON object per line, ``stdout`` and
``stderr`` lines as the command prints them, a ``keepalive`` while the command
is silent, and one final ``exit``. Output is never capped; only time is
bounded. The process is the desktop's concurrency group's, spawned as the
leader of its own process group, so the timeout, a dropped connection, or the
desktop shutting down kills everything it spawned.

Every command leaves two records in ``<data_dir>/events/remote_execution/events.jsonl``
(started, then finished), so a desktop killed mid-command still leaves the
record that the command began. The output itself is never logged.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Generator
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import format_nanosecond_iso_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.api_models import RemoteExecutionRequest
from imbue.minds.desktop_client.api_transport import ApiTransport
from imbue.minds.desktop_client.program_resolution import expand_program_home
from imbue.minds.errors import RemoteCommandRequestError

REMOTE_EXECUTION_EVENT_SOURCE: Final[EventSource] = EventSource("remote_execution")
COMMAND_STARTED_EVENT_TYPE: Final[EventType] = EventType("command_started")
COMMAND_FINISHED_EVENT_TYPE: Final[EventType] = EventType("command_finished")
# The events file, relative to the installation's data directory.
REMOTE_EXECUTION_EVENTS_RELATIVE_PATH: Final[Path] = Path("events") / "remote_execution" / "events.jsonl"

# The owner-exec /run default, so a command that gets no timeout of its own
# has the same ceiling an agent would meet on a VM.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 600.0
# A silent command's connection carries one of these per interval so the two
# node proxies and the reverse SSH tunnel between the agent and this desktop
# see traffic; readers of the owner-exec shape skip event types they do not know.
KEEPALIVE_INTERVAL_SECONDS: Final[float] = 30.0
# How long the process group is given to leave after SIGTERM before SIGKILL.
_TERMINATE_GRACE_SECONDS: Final[float] = 5.0
# How long a cancellation waits for the runner to report the kill done, past the grace above.
_CANCEL_WAIT_SECONDS: Final[float] = _TERMINATE_GRACE_SECONDS + 5.0
# A command that ran this long is logged at warning on completion.
_SLOW_COMMAND_WARNING_SECONDS: Final[float] = 60.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.25
# The exit codes a shell gives a command it could not start, used for the same failures here.
EXIT_CODE_COMMAND_NOT_FOUND: Final[int] = 127
EXIT_CODE_COMMAND_NOT_EXECUTABLE: Final[int] = 126

_STDOUT_STREAM: Final[str] = "stdout"
_STDERR_STREAM: Final[str] = "stderr"

# Commands run concurrently and each run has its own log object, so the one
# events file is guarded by one lock rather than one per writer.
_EVENTS_FILE_LOCK: Final[threading.Lock] = threading.Lock()


class RemoteCommandRequestCode(LowerCaseStrEnum):
    """Why a remote-execution body was refused before anything ran; the ``code`` of the 400, like the route's other codes."""

    CWD_NOT_A_DIRECTORY = auto()


class RemoteCommandRun(FrozenModel):
    """One command as the executor runs it: the request with its defaults and paths resolved."""

    command: tuple[str, ...] = Field(
        description="The argv to execute directly, its first element with a leading '~/' expanded to the home directory"
    )
    cwd: Path = Field(description="The resolved, existing working directory")
    timeout_seconds: float = Field(description="Seconds the command may run before it is killed")


class RemoteCommandAttribution(FrozenModel):
    """Who asked for a command, as far as the desktop can tell; recorded, never used to authorize."""

    claimed_agent_id: str | None = Field(
        description="The caller's self-declared agent label (X-Imbue-Studio-Agent), or None when it sent none"
    )
    workspace_id: str | None = Field(
        description="The workspace that label resolves to when discovery knows the agent; else None"
    )
    transport: ApiTransport = Field(description="How the request reached the desktop")


class RemoteCommandStartedEvent(EventEnvelope):
    """A command the desktop began running for an agent, as the log records it."""

    execution_id: str = Field(description="Shared by this command's started and finished records")
    claimed_agent_id: str | None = Field(description="The caller's self-declared agent label, or None")
    workspace_id: str | None = Field(description="The workspace the label resolved to, or None")
    transport: str = Field(description="How the request arrived: GATEWAY or LOCAL_SOCKET")
    command: tuple[str, ...] = Field(description="The argv as run (a '~/' in the first element expanded)")
    resolved_program: str | None = Field(
        default=None,
        description="The file the first argv element resolved to on this computer, or None when it did not",
    )
    cwd: str = Field(description="The working directory the command ran in")
    timeout_seconds: float = Field(description="The timeout the command ran under")


class RemoteCommandFinishedEvent(EventEnvelope):
    """How a command the desktop ran for an agent ended."""

    execution_id: str = Field(description="Shared with the command's started record")
    exit_code: int | None = Field(description="The process exit code; None when it was killed")
    timed_out: bool = Field(description="Whether the timeout killed it")
    cancelled: bool = Field(description="Whether the agent's dropped connection, or the desktop's shutdown, killed it")
    duration_seconds: float = Field(description="Wall-clock seconds from start to the process ending")
    stdout_bytes: int = Field(description="How many bytes it printed on standard output")
    stderr_bytes: int = Field(description="How many bytes it printed on standard error")


class StartFailure(FrozenModel):
    """Why a command could not be started at all, reported on the stream the way a shell would."""

    message: str = Field(description="The one line written to the stream's stderr")
    exit_code: int = Field(description="127 for a command that is not there, 126 for one that cannot run")


def resolve_remote_command(request: RemoteExecutionRequest, home_dir: Path) -> RemoteCommandRun:
    """The request with its defaults filled in and its working directory resolved.

    Raises :class:`RemoteCommandRequestError` when ``cwd`` is not an existing
    directory as this user sees it, which is the user the command would run as.
    """
    raw_cwd = request.cwd
    if raw_cwd is None:
        cwd = home_dir
    elif raw_cwd == "~" or raw_cwd.startswith("~/"):
        cwd = home_dir / raw_cwd[2:] if raw_cwd != "~" else home_dir
    else:
        cwd = Path(raw_cwd)
    if not cwd.is_absolute() or not cwd.is_dir():
        raise RemoteCommandRequestError(
            f"'cwd' must be an existing directory, absolute or '~'-rooted; got {raw_cwd!r}",
            code=RemoteCommandRequestCode.CWD_NOT_A_DIRECTORY,
            field="cwd",
        )
    timeout_seconds = request.timeout_seconds if request.timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
    # Only the program is expanded: the arguments are the command's own to interpret.
    command = (expand_program_home(request.command[0], home_dir), *request.command[1:])
    return RemoteCommandRun(command=command, cwd=cwd, timeout_seconds=timeout_seconds)


def resolve_executable_path(executable: str, environment: Mapping[str, str], cwd: Path) -> str | None:
    """The file the command's executable names, resolved the way the shell would, or None when there is none.

    A path with a slash is taken relative to ``cwd``, which is where the spawn
    would look for it; a bare name is looked up on the environment's PATH.
    """
    if "/" in executable:
        candidate_path = cwd / executable
        return str(candidate_path) if candidate_path.exists() else None
    return shutil.which(executable, path=environment.get("PATH"))


def describe_start_failure(executable: str, environment: Mapping[str, str], cwd: Path) -> StartFailure | None:
    """Why the command's executable cannot run, or None when it can.

    Checked before spawning so a missing or unrunnable program is reported on
    the stream with the shell's own exit codes rather than as a spawn error.
    """
    candidate = resolve_executable_path(executable, environment, cwd)
    if candidate is None:
        return StartFailure(message=f"command not found: {executable}\n", exit_code=EXIT_CODE_COMMAND_NOT_FOUND)
    if os.path.isdir(candidate) or not os.access(candidate, os.X_OK):
        return StartFailure(message=f"cannot execute: {executable}\n", exit_code=EXIT_CODE_COMMAND_NOT_EXECUTABLE)
    return None


def describe_spawn_failure(error: ProcessSetupError) -> StartFailure:
    """The start failure a spawn the pre-check let through still raised, with the OS error as the stderr line.

    An executable whose shebang names a missing interpreter fails at exec with
    ENOENT (a shell says 127); anything else the OS refuses to run (ENOEXEC,
    EACCES) gets the shell's 126.
    """
    exit_code = (
        EXIT_CODE_COMMAND_NOT_FOUND
        if isinstance(error.__cause__, FileNotFoundError)
        else EXIT_CODE_COMMAND_NOT_EXECUTABLE
    )
    return StartFailure(message=f"{error.stderr}\n", exit_code=exit_code)


@pure
def _event_line(event_type: str, **fields: object) -> str:
    return json.dumps({"type": event_type, **fields}) + "\n"


class RemoteCommandLog(MutableModel):
    """The append-only record of every command run for an agent, one started and one finished line each."""

    events_path: Path = Field(frozen=True, description="The JSONL file, created with mode 0600 on first write")

    def append(self, event: EventEnvelope) -> None:
        line = event.model_dump_json() + "\n"
        with _EVENTS_FILE_LOCK:
            try:
                self.events_path.parent.mkdir(parents=True, exist_ok=True)
                # Created 0600 like the rest of the per-install state: the argv
                # may carry something the agent typed that the user would not
                # want other accounts on the computer to read.
                file_descriptor = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(file_descriptor, "a") as events_file:
                    events_file.write(line)
            except OSError as e:
                logger.warning("Could not record a remote command to {}: {}", self.events_path, e)


class RemoteCommandExecution(MutableModel):
    """One command's run: starts the process, pumps its lines into a stream, and kills it when it must.

    ``stream_events`` is the whole lifecycle. The process belongs to the
    desktop's concurrency group and leads its own process group, so the
    timeout (the group's runner enforces it), a consumer that stops reading
    (the agent's connection dropped), and the group's shutdown each kill the
    command and everything it spawned; the finished record says which.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: RemoteCommandRun = Field(frozen=True, description="What to run")
    attribution: RemoteCommandAttribution = Field(frozen=True, description="Who asked, as recorded")
    log: RemoteCommandLog = Field(frozen=True, description="Where the started and finished records go")
    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="Owns the process and its reader thread")
    environment: Mapping[str, str] = Field(frozen=True, description="The environment the command runs with")
    keepalive_interval_seconds: float = Field(
        default=KEEPALIVE_INTERVAL_SECONDS, frozen=True, description="How often a silent stream carries a line"
    )
    _process: RunningProcess | None = PrivateAttr(default=None)
    _is_cancelled: bool = PrivateAttr(default=False)
    _bytes_by_stream: dict[str, int] = PrivateAttr(default_factory=lambda: {_STDOUT_STREAM: 0, _STDERR_STREAM: 0})

    def stream_events(self) -> Generator[str, None, None]:
        """Yield the NDJSON lines of one run, from the first line to the ``exit`` line."""
        execution_id = f"rce-{uuid4().hex}"
        started_at = time.monotonic()
        self.log.append(
            self._started_event(
                execution_id, resolve_executable_path(self.run.command[0], self.environment, self.run.cwd)
            )
        )
        exit_code: int | None = None
        is_timed_out = False
        try:
            start_failure = describe_start_failure(self.run.command[0], self.environment, self.run.cwd)
            if start_failure is not None:
                exit_code = start_failure.exit_code
                yield from self._report_start_failure(start_failure)
                return
            lines: queue.Queue[tuple[str, bool]] = queue.Queue()
            try:
                self._process = self.concurrency_group.run_process_in_background(
                    list(self.run.command),
                    timeout=self.run.timeout_seconds,
                    is_checked_by_group=False,
                    on_output=lambda line, is_stdout: lines.put((line, is_stdout)),
                    cwd=self.run.cwd,
                    env=self.environment,
                    name=f"remote command for {self.attribution.claimed_agent_id or 'an unlabelled agent'}",
                    is_output_accumulated=False,
                    shutdown_timeout_sec=_TERMINATE_GRACE_SECONDS,
                    is_own_process_group=True,
                )
            except ProcessSetupError as e:
                spawn_failure = describe_spawn_failure(e)
                exit_code = spawn_failure.exit_code
                yield from self._report_start_failure(spawn_failure)
                return
            yield from self._pump(self._process, lines)
            exit_code, is_timed_out = self._exit_outcome(self._process)
            yield _event_line("exit", code=exit_code, timed_out=is_timed_out)
        finally:
            self._finish(execution_id, started_at, exit_code, is_timed_out)

    def _exit_outcome(self, process: RunningProcess) -> tuple[int | None, bool]:
        """The ended process's exit code (None when it was killed) and whether the timeout killed it."""
        is_timed_out = process.get_timed_out()
        if self.concurrency_group.is_shutting_down():
            self._is_cancelled = True
        exit_code = None if is_timed_out or self._is_cancelled else process.returncode
        return exit_code, is_timed_out

    def _report_start_failure(self, failure: StartFailure) -> Generator[str, None, None]:
        """Yield the two lines a command that never ran gets: its failure on stderr, then the shell's exit code."""
        yield self._line_event(failure.message, is_stdout=False)
        yield _event_line("exit", code=failure.exit_code, timed_out=False)

    def _pump(self, process: RunningProcess, lines: queue.Queue[tuple[str, bool]]) -> Generator[str, None, None]:
        """Yield line and keepalive events until the process has ended and every line it printed is out."""
        last_emitted_at = time.monotonic()
        while not process.is_finished():
            try:
                line, is_stdout = lines.get(timeout=_POLL_INTERVAL_SECONDS)
            except queue.Empty:
                if time.monotonic() - last_emitted_at >= self.keepalive_interval_seconds:
                    yield _event_line("keepalive")
                    last_emitted_at = time.monotonic()
                continue
            yield self._line_event(line, is_stdout)
            last_emitted_at = time.monotonic()
        # The runner hands every line over before it reports the process finished,
        # so what is queued now is all there will be.
        while not lines.empty():
            line, is_stdout = lines.get_nowait()
            yield self._line_event(line, is_stdout)

    def _line_event(self, line: str, is_stdout: bool) -> str:
        stream_name = _STDOUT_STREAM if is_stdout else _STDERR_STREAM
        self._bytes_by_stream[stream_name] += len(line.encode())
        return _event_line(stream_name, data=line)

    def _finish(self, execution_id: str, started_at: float, exit_code: int | None, is_timed_out: bool) -> None:
        """Kill a still-running process (the consumer went away) and record the end."""
        process = self._process
        if process is not None and not process.is_finished():
            self._is_cancelled = True
            try:
                process.terminate(force_kill_seconds=_CANCEL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Remote command {} did not stop within {}s of being cancelled", execution_id, _CANCEL_WAIT_SECONDS
                )
        if process is not None and exit_code is None and not is_timed_out and not self._is_cancelled:
            # The consumer left after the process ended on its own but before the exit line was
            # built, so the outcome is read from the process rather than recorded as a kill.
            exit_code, is_timed_out = self._exit_outcome(process)
        duration_seconds = time.monotonic() - started_at
        if duration_seconds > _SLOW_COMMAND_WARNING_SECONDS:
            logger.warning(
                "Remote command {} ran for {:.1f}s (slow): {}", execution_id, duration_seconds, self.run.command[0]
            )
        self.log.append(
            RemoteCommandFinishedEvent(
                timestamp=_now(),
                type=COMMAND_FINISHED_EVENT_TYPE,
                event_id=_new_event_id(),
                source=REMOTE_EXECUTION_EVENT_SOURCE,
                execution_id=execution_id,
                exit_code=exit_code,
                timed_out=is_timed_out,
                cancelled=self._is_cancelled,
                duration_seconds=duration_seconds,
                stdout_bytes=self._bytes_by_stream[_STDOUT_STREAM],
                stderr_bytes=self._bytes_by_stream[_STDERR_STREAM],
            )
        )

    def _started_event(self, execution_id: str, resolved_program: str | None) -> RemoteCommandStartedEvent:
        return RemoteCommandStartedEvent(
            timestamp=_now(),
            type=COMMAND_STARTED_EVENT_TYPE,
            event_id=_new_event_id(),
            source=REMOTE_EXECUTION_EVENT_SOURCE,
            execution_id=execution_id,
            claimed_agent_id=self.attribution.claimed_agent_id,
            workspace_id=self.attribution.workspace_id,
            transport=str(self.attribution.transport),
            command=self.run.command,
            resolved_program=resolved_program,
            cwd=str(self.run.cwd),
            timeout_seconds=self.run.timeout_seconds,
        )


def _now() -> IsoTimestamp:
    return IsoTimestamp(format_nanosecond_iso_timestamp(datetime.now(timezone.utc)))


def _new_event_id() -> EventId:
    return EventId(f"rce-{uuid4().hex}")
