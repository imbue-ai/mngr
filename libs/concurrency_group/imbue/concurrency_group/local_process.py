from __future__ import annotations

import contextvars
from pathlib import Path
from queue import Empty
from queue import Queue
from subprocess import TimeoutExpired
from threading import Event
from typing import Iterator
from typing import Mapping
from typing import Sequence
from typing import TypeVar

from imbue.concurrency_group.errors import EnvironmentStoppedError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.event_utils import MutableEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.concurrency_group.thread_utils import ObservableThread


class RunningProcess:
    """Represents a process running in the background."""

    def __init__(
        self,
        command: Sequence[str],
        output_queue: Queue[tuple[str, bool]] | None,
        shutdown_event: MutableEvent,
        is_checked: bool = False,
    ) -> None:
        self._command = command
        self._output_queue = output_queue
        self._shutdown_event = shutdown_event
        self._is_checked = is_checked
        self._completed_process: FinishedProcess | None = None
        self._thread: ObservableThread | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []

    def read_stdout(self) -> str:
        return "".join(self._stdout_lines)

    def stream_stdout_and_stderr(self) -> Iterator[tuple[str, bool]]:
        """Iterator that yields lines from the process output queue. Each item is (line, is_stdout)."""
        output_queue = self.get_queue()

        while not self._shutdown_event.is_set():
            try:
                line, is_stdout = output_queue.get(timeout=0.1)
                yield line, is_stdout
            except Empty:
                if self.poll() is not None:
                    break

    def read_stderr(self) -> str:
        return "".join(self._stderr_lines)

    def get_queue(self) -> Queue[tuple[str, bool]]:
        assert self._output_queue is not None, "Output queue must be set to get the queue for RunningProcess"
        return self._output_queue

    @property
    def returncode(self) -> int | None:
        return self.poll()

    @property
    def is_checked(self) -> bool:
        return self._is_checked

    @property
    def command(self) -> Sequence[str]:
        """Human-readable command string."""
        return self._command

    def wait_and_read(self, timeout: float | None = None) -> tuple[str, str]:
        self.wait(timeout)
        return self.read_stdout(), self.read_stderr()

    def wait(self, timeout: float | None = None) -> int:
        thread = self._thread
        assert thread is not None, "Thread must be started before waiting"
        if thread.is_alive():
            thread.join(timeout)
        if thread.is_alive():
            stdout = self.read_stdout()
            stderr = self.read_stderr()
            raise TimeoutExpired(self._command, timeout if timeout is not None else 0.0, stdout, stderr)
        result = self.poll()
        if result is None:
            raise ProcessSetupError(
                command=tuple(self._command),
                stdout="",
                stderr="Process exited before being started!",
                is_output_already_logged=True,
            )
        if self._is_checked:
            self.check()
        return result

    def check(self) -> None:
        if self.returncode is not None and self.returncode != 0:
            stdout, stderr = self.read_stdout(), self.read_stderr()
            raise ProcessError(tuple(self._command), stdout, stderr, self.returncode)

    def poll(self) -> int | None:
        thread = self._thread
        if thread is None or thread.native_id is None:
            return None
        if self._completed_process is not None:
            return self._completed_process.returncode

        if not thread.is_alive():
            if self._completed_process is not None:
                return self._completed_process.returncode
            if thread.exception_raw is not None:
                thread.join()
            return 1007

        return None

    def is_finished(self) -> bool:
        try:
            return self.poll() is not None
        except ProcessSetupError:
            return True

    def terminate(self, force_kill_seconds: float = 5.0) -> None:
        self._shutdown_event.set()
        thread = self._thread
        assert thread is not None
        thread.join(timeout=force_kill_seconds)
        if thread.is_alive():
            stdout = self.read_stdout()
            stderr = self.read_stderr()
            raise TimeoutExpired(self._command, force_kill_seconds, stdout, stderr)

    def start(self, kwargs: dict) -> None:
        context = contextvars.copy_context()
        queue: Queue[BaseException | None] = Queue(maxsize=1)

        def on_initialized(maybe_exception):
            return queue.put_nowait(maybe_exception)

        self._thread = ObservableThread(
            target=lambda: context.run(self.run, {**kwargs, "on_initialization_complete": on_initialized}),
            name=self._get_name(),
            silenced_exceptions=(ProcessError, EnvironmentStoppedError),
        )
        self._thread.start()
        maybe_initialization_exception = queue.get()
        if maybe_initialization_exception is not None:
            raise maybe_initialization_exception

    def _get_name(self) -> str:
        return f"RunningProcess: {' '.join(self._command)}"

    def run(self, kwargs: dict) -> None:
        self._completed_process = run_local_command_modern_version(**kwargs)

    def get_timed_out(self) -> bool:
        if self._completed_process is None:
            return False
        return self._completed_process.is_timed_out

    def on_line(self, line: str, is_stdout: bool) -> None:
        if is_stdout:
            self._stdout_lines.append(line)
        else:
            self._stderr_lines.append(line)
        if self._output_queue is not None:
            self._output_queue.put((line, is_stdout))


ProcessClassType = TypeVar("ProcessClassType", bound=RunningProcess)


def run_background(
    command: Sequence[str],
    output_queue: Queue[tuple[str, bool]] | None = None,
    timeout: float | None = None,
    is_checked: bool = False,
    cwd: Path | None = None,
    shutdown_event: MutableEvent | None = None,
    shutdown_timeout_sec: float = 30.0,
    env: Mapping[str, str] | None = None,
    process_class: type[ProcessClassType] = RunningProcess,  # type: ignore[assignment]
    process_class_kwargs: Mapping[str, object] | None = None,
) -> ProcessClassType:
    """
    Run a subprocess command in a non-blocking manner with output handling.

    Returns immediately with a RunningProcess object that allows the caller to:
    - Access a queue to process output lines as they are produced
    - Wait for completion and read all output at once
    - Check process status, terminate it, or monitor return codes
    """
    if output_queue is None:
        output_queue = Queue()
    true_shutdown_event = shutdown_event if shutdown_event is not None else Event()
    process = process_class(
        output_queue=output_queue,
        shutdown_event=true_shutdown_event,
        command=command,
        is_checked=is_checked,
        **(process_class_kwargs or {}),
    )
    process.start(
        kwargs=dict(
            command=command,
            is_checked=False,
            timeout=timeout,
            trace_output=bool(process.on_line),
            cwd=cwd,
            trace_on_line_callback=process.on_line,
            shutdown_event=true_shutdown_event,
            shutdown_timeout_sec=shutdown_timeout_sec,
            env=env,
        )
    )
    return process
