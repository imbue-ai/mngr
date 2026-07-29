from __future__ import annotations

import contextvars
from pathlib import Path
from queue import Queue
from subprocess import TimeoutExpired
from threading import Event
from typing import Mapping
from typing import Sequence
from typing import TypeVar

from imbue.concurrency_group.errors import EnvironmentStoppedError
from imbue.concurrency_group.errors import OutputNotAccumulatedError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.event_utils import MutableEvent
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import OUTPUT_NOT_ACCUMULATED_PLACEHOLDER
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
        name: str | None = None,
        is_output_accumulated: bool = True,
    ) -> None:
        self._command = command
        # An optional caller-supplied, log-safe label for this process. Used as
        # the reader thread's name and as the display command in any
        # ProcessError/TimeoutExpired it raises, so callers whose command
        # carries secret argument values can keep those out of logs. The real
        # ``command`` is still what gets executed. Defaults (``None``) to the
        # joined command, preserving prior behavior.
        self._name = name
        # Only ever written to (in ``on_line``); this class exposes no way to read it
        # back. It is therefore useful solely to a caller that supplied its own queue
        # and holds its own reference. ``None`` means "nobody is listening", and
        # ``run_background`` deliberately leaves it that way by default -- populating
        # a queue no one can drain would retain every output line forever.
        self._output_queue = output_queue
        self._shutdown_event = shutdown_event
        self._is_checked = is_checked
        self._completed_process: FinishedProcess | None = None
        self._thread: ObservableThread | None = None
        # When False, these stay empty: a process that runs for days would otherwise
        # retain every line it ever printed. Callers in that mode consume output as it
        # arrives (via the output queue or an on-line callback) and must not read it
        # back afterwards -- ``read_stdout``/``read_stderr`` raise instead.
        self._is_output_accumulated = is_output_accumulated
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []

    @property
    def is_output_accumulated(self) -> bool:
        """Whether this process keeps a record of its output for later reading."""
        return self._is_output_accumulated

    def read_stdout(self) -> str:
        self._raise_if_output_is_not_accumulated("read_stdout")
        return "".join(self._stdout_lines)

    def read_stderr(self) -> str:
        self._raise_if_output_is_not_accumulated("read_stderr")
        return "".join(self._stderr_lines)

    def _raise_if_output_is_not_accumulated(self, method_name: str) -> None:
        if not self._is_output_accumulated:
            raise OutputNotAccumulatedError(
                f"Cannot call {method_name}() on `{self._get_name()}`: it was started with "
                "is_output_accumulated=False, so its output was never recorded. Consume output as "
                "it arrives (via on_output / an output_queue) instead."
            )

    def _read_output_for_error(self) -> tuple[str, str]:
        """Return (stdout, stderr) to embed in a raised error; never raises itself.

        Error paths must keep reporting the failure they were built for even when this
        process kept no output, so they get an explicit placeholder rather than the
        ``OutputNotAccumulatedError`` the public readers raise.
        """
        if not self._is_output_accumulated:
            return OUTPUT_NOT_ACCUMULATED_PLACEHOLDER, OUTPUT_NOT_ACCUMULATED_PLACEHOLDER
        return self.read_stdout(), self.read_stderr()

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
            stdout, stderr = self._read_output_for_error()
            raise TimeoutExpired(self._error_display, timeout if timeout is not None else 0.0, stdout, stderr)
        result = self.poll()
        if result is None:
            raise ProcessSetupError(
                command=tuple(self._command),
                stdout="",
                stderr="Process exited before being started!",
                is_output_already_logged=True,
                display_name=self._name,
            )
        if self._is_checked:
            self.check()
        return result

    def check(self) -> None:
        if self.returncode is not None and self.returncode != 0:
            stdout, stderr = self._read_output_for_error()
            raise ProcessError(tuple(self._command), stdout, stderr, self.returncode, display_name=self._name)

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
            stdout, stderr = self._read_output_for_error()
            raise TimeoutExpired(self._error_display, force_kill_seconds, stdout, stderr)

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
        if self._name is not None:
            return self._name
        return f"RunningProcess: {' '.join(self._command)}"

    @property
    def _error_display(self) -> Sequence[str] | str:
        """The label to show for this process in a raised error (the ``name`` when supplied).

        ``subprocess.TimeoutExpired`` renders its ``cmd`` into ``str(exc)``, so a
        command carrying secrets would leak there. When a ``name`` was supplied we
        hand that label in instead; otherwise we fall back to the real command
        (prior behavior). The value is a plain display label, not necessarily a
        command -- hence the name.
        """
        return self._name if self._name is not None else self._command

    def run(self, kwargs: dict) -> None:
        self._completed_process = run_local_command_modern_version(**kwargs)

    def get_timed_out(self) -> bool:
        if self._completed_process is None:
            return False
        return self._completed_process.is_timed_out

    def on_line(self, line: str, is_stdout: bool) -> None:
        if self._is_output_accumulated:
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
    # Open file descriptors to keep open in (and inherit into) the spawned child, by their fd numbers.
    pass_fds: Sequence[int] = (),
    process_class: type[ProcessClassType] = RunningProcess,  # ty: ignore[invalid-parameter-default]
    process_class_kwargs: Mapping[str, object] | None = None,
    name: str | None = None,
    is_output_accumulated: bool = True,
) -> ProcessClassType:
    """
    Run a subprocess command in a non-blocking manner with output handling.

    Returns immediately with a RunningProcess object that allows the caller to:
    - Wait for completion and read all output at once
    - Check process status, terminate it, or monitor return codes

    To observe output lines as they are produced, pass your own ``output_queue`` (and
    drain it) or use ``ConcurrencyGroup.run_process_in_background``'s ``on_output``
    callback. When ``output_queue`` is omitted none is allocated: ``RunningProcess``
    exposes no way to read a queue back, so one the caller did not supply could never
    be drained and would simply retain every output line for the process's lifetime.

    ``name`` is an optional log-safe label for the process (see ``RunningProcess``).

    ``is_output_accumulated=False`` discards output once it has been handed to the queue /
    line callback, instead of retaining all of it for later ``read_stdout()``. Use it for
    processes that stream for a long time, where the full history is both unwanted and
    unbounded; ``read_stdout()``/``read_stderr()`` then raise ``OutputNotAccumulatedError``.
    """
    true_shutdown_event = shutdown_event if shutdown_event is not None else Event()
    process = process_class(
        output_queue=output_queue,
        shutdown_event=true_shutdown_event,
        command=command,
        is_checked=is_checked,
        name=name,
        is_output_accumulated=is_output_accumulated,
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
            pass_fds=pass_fds,
            name=name,
            is_output_accumulated=is_output_accumulated,
        )
    )
    return process
