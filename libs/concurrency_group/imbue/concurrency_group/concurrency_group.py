import time
from collections import defaultdict
from contextlib import AbstractContextManager
from enum import auto
from functools import wraps
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Lock
from typing import Any
from typing import Callable
from typing import Concatenate
from typing import Final
from typing import Mapping
from typing import ParamSpec
from typing import Sequence
from typing import TypeVar

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.event_utils import ReadOnlyEvent
from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.local_process import run_background
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.mutable_model import MutableModel

P = ParamSpec("P")
T = TypeVar("T")

DEFAULT_EXIT_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 10.0

# Increase this if cleanup becomes a performance bottleneck.
CLEANUP_INTERVAL_TICKS: Final[int] = 1
# For each kind of strand, we don't need to keep too many failed ones around after cleanup.
MAX_FAILED_STRANDS_TO_KEEP_AFTER_CLEANUP: Final[int] = 8


def _raise_if_any_strands_or_ancestors_failed_or_is_shutting_down(
    func: Callable[Concatenate["ConcurrencyGroup", P], T],
) -> Callable[Concatenate["ConcurrencyGroup", P], T]:
    @wraps(func)
    def wrapper(self: "ConcurrencyGroup", *args: P.args, **kwargs: P.kwargs) -> T:
        self.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()
        return func(self, *args, **kwargs)

    return wrapper


def _trigger_cleanup(
    func: Callable[Concatenate["ConcurrencyGroup", P], T],
) -> Callable[Concatenate["ConcurrencyGroup", P], T]:
    @wraps(func)
    def wrapper(self: "ConcurrencyGroup", *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            result = func(self, *args, **kwargs)
        finally:
            if self._cleanup_tick():
                self._cleanup()
        return result

    return wrapper


class ConcurrencyGroupState(UpperCaseStrEnum):
    """Represents the lifecycle state of a concurrency group."""

    INSTANTIATED = auto()
    ACTIVE = auto()
    EXITING = auto()
    EXITED = auto()


class _TrackedThread(MutableModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thread: ObservableThread
    is_checked: bool


class ConcurrencyGroup(MutableModel, AbstractContextManager):
    """
    A context manager to manage threads and processes.

    - Keep track of threads and processes created within the context manager.
    - Ensure that they are cleaned up properly and their failures are handled.
    - Keep track of nested concurrency groups.
    - Propagate shutdown events to all threads and processes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Use a descriptive name for easier debugging.
    name: str
    shutdown_event: ShutdownEvent = Field(default_factory=ShutdownEvent.build_root)
    # How long to wait for strands to finish when exiting the context manager.
    exit_timeout_seconds: float = DEFAULT_EXIT_TIMEOUT_SECONDS
    # How long to wait for strands to finish when shutting down the whole application.
    shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    parent: "ConcurrencyGroup | None" = None
    _state: ConcurrencyGroupState = PrivateAttr(default=ConcurrencyGroupState.INSTANTIATED)

    _threads: list[_TrackedThread] = PrivateAttr(default_factory=list)
    _processes: list[RunningProcess] = PrivateAttr(default_factory=list)

    _lock: Lock = PrivateAttr(default_factory=Lock)
    _children: list["ConcurrencyGroup"] = PrivateAttr(default_factory=list)

    # Did the concurrency group already exit with an exception?
    _exit_exception: BaseException | None = PrivateAttr(default=None)

    # Is the concurrency group still active but has already noticed that it operates in a failed context?
    _pre_exit_exception: Exception | None = PrivateAttr(default=None)

    # We periodically clean up finished strands every CLEANUP_INTERVAL_TICKS calls to strand-starting methods.
    _cleanup_tick_counter: int = PrivateAttr(default=0)
    _cleanup_lock: Lock = PrivateAttr(default_factory=Lock)

    def __enter__(self) -> "ConcurrencyGroup":
        with self._lock:
            if self._state != ConcurrencyGroupState.INSTANTIATED:
                raise InvalidConcurrencyGroupStateError(
                    f"This concurrency group has been already activated (`{self.name}`)."
                )
            self._state = ConcurrencyGroupState.ACTIVE
        return self

    def __exit__(self, exc_type: type | None, exc_value: BaseException | None, traceback: Any) -> None:
        try:
            with self._lock:
                self._state = ConcurrencyGroupState.EXITING
            self._exit(exc_value)
        except BaseException as exit_exception:
            self._exit_exception = exit_exception
            raise
        finally:
            self._state = ConcurrencyGroupState.EXITED

    def _exit(self, exc_value: BaseException | None) -> None:
        main_exception: BaseException | None = exc_value if exc_value is not None else None
        timeout_exception_group: ConcurrencyExceptionGroup | None = None
        failure_exception_group: ConcurrencyExceptionGroup | None = None

        try:
            if self.is_shutting_down():
                self._wait_for_all_strands_to_finish_with_timeout(self.shutdown_timeout_seconds)
            else:
                self._wait_for_all_strands_to_finish_with_timeout(self.exit_timeout_seconds)
        except ConcurrencyExceptionGroup as exception_group:
            timeout_exception_group = exception_group

        try:
            self._raise_if_any_strands_or_ancestors_failed()
        except ConcurrencyExceptionGroup as exception_group:
            failure_exception_group = exception_group

        exceptions = []
        message: str | None = None
        if timeout_exception_group is not None:
            exceptions.extend(timeout_exception_group.exceptions)
            message = timeout_exception_group.message
        if failure_exception_group is not None:
            exceptions.extend(failure_exception_group.exceptions)
            message = failure_exception_group.message
        if main_exception is not None:
            if isinstance(main_exception, ConcurrencyExceptionGroup):
                exceptions.extend(main_exception.exceptions)
                message = main_exception.message
            else:
                exceptions.append(main_exception)
                message = str(main_exception)

        for child in self._children:
            if child.state not in (ConcurrencyGroupState.EXITED, ConcurrencyGroupState.INSTANTIATED):
                child_message = f"A child concurrency group did not exit: `{child.name}` (state: {child.state})."
                exceptions.append(ChildConcurrencyGroupDidNotExitError(child_message))
                message = message or child_message

        for exception in exceptions:
            if not isinstance(exception, Exception):
                raise exception
        assert main_exception is None or isinstance(main_exception, Exception)

        if len(exceptions) > 0:
            exceptions = _deduplicate_exceptions(tuple(exceptions))
            assert message is not None
            if main_exception is not None:
                raise ConcurrencyExceptionGroup(message, exceptions, main_exception=main_exception) from main_exception
            raise ConcurrencyExceptionGroup(message, exceptions)

    def _wait_for_all_strands_to_finish_with_timeout(self, timeout_seconds: float) -> None:
        start_time = time.monotonic()
        timeout_errors: list[StrandTimedOutError] = []
        setup_errors: list[ProcessSetupError] = []
        for process in self._processes:
            if not process.is_finished():
                remaining_timeout = self._get_remaining_timeout(start_time, timeout_seconds)
                try:
                    process.wait(timeout=remaining_timeout)
                except ProcessSetupError as error:
                    setup_errors.append(error)
                except TimeoutExpired as error:
                    command = error.cmd
                    stdout = process.read_stdout()[:1024]
                    stderr = process.read_stderr()[:1024]
                    message = "\n".join(
                        [
                            f"Process {command} did not terminate in time and was killed.",
                            f"Stdout: {stdout}",
                            f"Stderr: {stderr}",
                        ]
                    )
                    try:
                        raise StrandTimedOutError(message) from error
                    except StrandTimedOutError as e:
                        timeout_errors.append(e)
                    try:
                        process.terminate(force_kill_seconds=0.0)
                    except TimeoutExpired:
                        pass
        for tracked_thread in self._threads:
            remaining_timeout = self._get_remaining_timeout(start_time, timeout_seconds)
            # Thread.join(timeout=float("inf")) raises OverflowError, so convert to None (wait forever)
            join_timeout = None if remaining_timeout == float("inf") else remaining_timeout
            try:
                tracked_thread.thread.join(timeout=join_timeout)
            except Exception:
                pass
            if tracked_thread.thread.is_alive():
                message = (
                    f"Thread `{tracked_thread.thread.name} ({tracked_thread.thread.target_name})` "
                    f"did not finish in time and is still alive."
                )
                try:
                    raise StrandTimedOutError(message)
                except StrandTimedOutError as e:
                    timeout_errors.append(e)
        if len(timeout_errors) > 0 or len(setup_errors) > 0:
            message = f"{self.name}: "
            if len(timeout_errors) > 0:
                message += f"{len(timeout_errors)} strands did not finish in time and were terminated. "
            if len(setup_errors) > 0:
                message += f"{len(setup_errors)} strands could not be started."
            exceptions = timeout_errors + setup_errors
            if len(exceptions) == 1:
                message += f"\n{exceptions[0]}"
            raise ConcurrencyExceptionGroup(message, exceptions)

    def _get_remaining_timeout(self, start_time_seconds: float, total_timeout_seconds: float) -> float:
        elapsed_seconds = time.monotonic() - start_time_seconds
        return max(0, total_timeout_seconds - elapsed_seconds)

    def _raise_if_not_active(self) -> None:
        if self._state != ConcurrencyGroupState.ACTIVE:
            raise InvalidConcurrencyGroupStateError(
                f"Concurrency group `{self.name}` not active: the state is {self._state}."
            )

    def _raise_if_any_strands_or_ancestors_failed(self) -> None:
        exceptions = []
        with self._lock:
            threads = self._threads[:]
            processes = self._processes[:]
        for tracked_thread in threads:
            if not tracked_thread.is_checked:
                continue
            try:
                tracked_thread.thread.maybe_raise()
            except Exception as e:
                exceptions.append(e)
        for process in processes:
            if not process.is_checked:
                continue
            try:
                process.check()
            except ProcessError as e:
                exceptions.append(e)
        ancestor_exception = self._maybe_get_closest_ancestor_exception()
        if ancestor_exception is not None:
            if not isinstance(ancestor_exception, Exception):
                raise ancestor_exception
            exceptions.append(AncestorConcurrentFailure(ancestor_exception))
        if len(exceptions) > 0:
            message = f"{len(exceptions)} strands failed in concurrency group `{self.name}`."
            if len(exceptions) == 1 and isinstance(exceptions[0], ProcessError):
                output = exceptions[0].stdout[:128] + " (...) " + exceptions[0].stderr[:128]
                message += f"\nFailed command: {exceptions[0].command}\nOutput: {output}"
            raise ConcurrencyExceptionGroup(
                message,
                exceptions,
            )

    def raise_if_any_strands_or_ancestors_failed_or_is_shutting_down(self) -> None:
        """
        Check all the registered strands and raise an exception if any of them failed.

        Also check if the parent concurrency group failed (to propagate failures sideways and downwards).
        Also check if the concurrency group is shutting down and raise an exception if it is.

        This method is public because you might want to call it from within the context manager to see if there were
        any failures so far.
        """
        exceptions = []
        main_exception: Exception | None = None
        message: str | None = None
        try:
            self._raise_if_any_strands_or_ancestors_failed()
        except ConcurrencyExceptionGroup as exception_group:
            exceptions.extend(exception_group.exceptions)
            main_exception = exception_group.main_exception
            self._pre_exit_exception = exception_group
            message = exception_group.message

        if self.is_shutting_down():
            try:
                raise ConcurrentShutdownError(f"The concurrency group is shutting down: `{self.name}`.")
            except ConcurrentShutdownError as e:
                exceptions.append(e)

        if len(exceptions) > 0:
            raise ConcurrencyExceptionGroup(
                message or f"{len(exceptions)} detected failures in concurrency group: `{self.name}`.",
                exceptions,
                main_exception=main_exception,
            )

    @property
    def unfinished_processes(self) -> tuple[RunningProcess, ...]:
        with self._lock:
            return tuple(process for process in self._processes if not process.is_finished())

    def is_shutting_down(self) -> bool:
        return self.shutdown_event.is_set()

    def _maybe_get_closest_ancestor_exception(self) -> BaseException | None:
        """Check if any ancestor concurrency group failed and return its exception."""
        current = self.parent
        while current is not None:
            if current.state == ConcurrencyGroupState.EXITED and current.exit_exception is not None:
                return current.exit_exception
            if (
                current.state in (ConcurrencyGroupState.EXITING, ConcurrencyGroupState.ACTIVE)
                and current._pre_exit_exception is not None
            ):
                return current._pre_exit_exception
            current = current.parent
        return None

    def _maybe_wrap_external_shutdown_event(self, external_shutdown_event: ReadOnlyEvent | None) -> ShutdownEvent:
        if external_shutdown_event is None:
            return ShutdownEvent.from_parent(self.shutdown_event)
        return ShutdownEvent.from_parent(self.shutdown_event, external=external_shutdown_event)

    @_trigger_cleanup
    @_raise_if_any_strands_or_ancestors_failed_or_is_shutting_down
    def start_thread(self, thread: ObservableThread, is_checked: bool = True) -> None:
        with self._lock:
            self._raise_if_not_active()
            thread.start()
            self._threads.append(_TrackedThread(thread=thread, is_checked=is_checked))

    @_trigger_cleanup
    @_raise_if_any_strands_or_ancestors_failed_or_is_shutting_down
    def start_new_thread(
        self,
        target: Callable[..., Any],
        args: tuple = (),
        kwargs: dict | None = None,
        name: str | None = None,
        daemon: bool = True,
        silenced_exceptions: tuple[type[BaseException], ...] | None = None,
        suppressed_exceptions: tuple[type[BaseException], ...] | None = None,
        is_checked: bool = True,
        on_failure: Callable[[BaseException], None] | None = None,
    ) -> ObservableThread:
        thread = ObservableThread(
            target=target,
            args=args,
            kwargs=kwargs,
            name=name,
            daemon=daemon,
            silenced_exceptions=silenced_exceptions,
            suppressed_exceptions=suppressed_exceptions,
            on_failure=on_failure,
        )
        self.start_thread(thread, is_checked)
        return thread

    @_trigger_cleanup
    @_raise_if_any_strands_or_ancestors_failed_or_is_shutting_down
    def start_background_process_from_factory(self, process_factory: Callable[[], RunningProcess]) -> RunningProcess:
        """Start a background process using the given factory."""
        with self._lock:
            self._raise_if_not_active()
            process = process_factory()
            self._processes.append(process)
        return process

    def run_process_in_background(
        self,
        command: Sequence[str],
        timeout: float | None = None,
        is_checked_by_group: bool = False,
        on_output: Callable[[str, bool], None] | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shutdown_event: ReadOnlyEvent | None = None,
    ) -> RunningProcess:
        """
        Run a process in the background, returning immediately.

        When `is_checked_by_group` is True, the process will be checked for failure when the concurrency group exits
        or whenever its methods are called.
        """

        def process_factory():
            return run_background(
                command,
                cwd=Path(cwd) if cwd is not None else None,
                env=env,
                is_checked=is_checked_by_group,
                timeout=timeout,
                shutdown_event=self._maybe_wrap_external_shutdown_event(shutdown_event),
                process_class=RunningProcessWithOnLineCallback,
                process_class_kwargs={"on_line_callback": on_output},
            )

        return self.start_background_process_from_factory(process_factory)

    def run_process_to_completion(
        self,
        command: Sequence[str],
        timeout: float | None = None,
        is_checked_after: bool = True,
        on_output: Callable[[str, bool], None] | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shutdown_event: ReadOnlyEvent | None = None,
    ) -> FinishedProcess:
        """
        Run a process to completion, blocking until it finishes.

        When `is_checked_after` is True (the default), raise a ProcessError if the process exits with a non-zero
        exit code.
        """
        process = self.run_process_in_background(
            command,
            timeout=timeout,
            cwd=cwd,
            env=env,
            shutdown_event=shutdown_event,
            on_output=on_output,
            is_checked_by_group=False,
        )
        process.wait()
        if is_checked_after:
            process.check()

        return FinishedProcess(
            command=tuple(process.command),
            returncode=process.returncode,
            stdout=process.read_stdout(),
            stderr=process.read_stderr(),
            is_timed_out=process.get_timed_out(),
            is_output_already_logged=False,
        )

    def _cleanup(self) -> None:
        """Clean up strands from the tracking lists."""
        with self._lock:
            threads = []
            processes = []
            failed_thread_count = 0
            failed_process_count = 0
            for tracked_thread in self._threads:
                if tracked_thread.thread.is_alive():
                    threads.append(tracked_thread)
                elif (
                    tracked_thread.is_checked
                    and tracked_thread.thread.exception_if_not_suppressed
                    and failed_thread_count < MAX_FAILED_STRANDS_TO_KEEP_AFTER_CLEANUP
                ):
                    failed_thread_count += 1
                    threads.append(tracked_thread)
            for process in self._processes:
                if not process.is_finished():
                    processes.append(process)
                elif (
                    process.is_checked
                    and failed_process_count < MAX_FAILED_STRANDS_TO_KEEP_AFTER_CLEANUP
                    and process.returncode not in (0, None)
                ):
                    failed_process_count += 1
                    processes.append(process)
            self._threads = threads
            self._processes = processes

    def _cleanup_tick(self) -> bool:
        with self._cleanup_lock:
            self._cleanup_tick_counter += 1
            return self._cleanup_tick_counter % CLEANUP_INTERVAL_TICKS == 0

    @_raise_if_any_strands_or_ancestors_failed_or_is_shutting_down
    def make_concurrency_group(
        self,
        name: str,
        exit_timeout_seconds: float = DEFAULT_EXIT_TIMEOUT_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> "ConcurrencyGroup":
        """
        Create a child concurrency group.

        The child concurrency group will be tracked by the parent and its state will be checked when the parent exits
        to verify it is no longer running or mid-exit.

        Also, the child concurrency group can see if any of its ancestors failed.
        """
        shutdown_event = ShutdownEvent.from_parent(self.shutdown_event)
        concurrency_group = ConcurrencyGroup(
            parent=self,
            name=name,
            exit_timeout_seconds=exit_timeout_seconds,
            shutdown_timeout_seconds=shutdown_timeout_seconds,
            shutdown_event=shutdown_event,
        )
        with self._lock:
            self._raise_if_not_active()
            self._children = [child for child in self._children if child.state != ConcurrencyGroupState.EXITED]
            self._children.append(concurrency_group)
        return concurrency_group

    @property
    def state(self) -> ConcurrencyGroupState:
        return self._state

    @property
    def exit_exception(self) -> BaseException | None:
        return self._exit_exception

    def shutdown(self) -> None:
        for child in self._children:
            child.shutdown()
        self.shutdown_event.set()


class RunningProcessWithOnLineCallback(RunningProcess):
    """RunningProcess subclass that supports an on_line callback."""

    def __init__(
        self,
        on_line_callback: Callable[[str, bool], None] | None = None,
        *args,
        **kwargs,
    ) -> None:
        self._on_line_callback = on_line_callback
        super().__init__(*args, **kwargs)

    def on_line(self, line: str, is_stdout: bool) -> None:
        super().on_line(line, is_stdout)
        if self._on_line_callback is not None:
            self._on_line_callback(line, is_stdout)


def _deduplicate_exceptions(exceptions: tuple[Exception, ...]) -> tuple[Exception, ...]:
    """Deduplicate accumulated exceptions."""
    exceptions = tuple(set(exceptions))
    process_error_buckets: dict[tuple, list[ProcessError]] = defaultdict(list)
    other_exceptions = []
    for exception in exceptions:
        if isinstance(exception, ProcessError):
            key = (exception.command, exception.returncode, exception.stdout, exception.stderr)
            process_error_buckets[key].append(exception)
        else:
            other_exceptions.append(exception)
    deduplicated_process_errors = []
    for bucket in process_error_buckets.values():
        with_traceback = [e for e in bucket if e.__traceback__ is not None]
        if len(with_traceback) > 0:
            deduplicated_process_errors.append(with_traceback[0])
        else:
            deduplicated_process_errors.append(bucket[0])
    return tuple(other_exceptions + deduplicated_process_errors)


class StrandTimedOutError(ConcurrencyGroupError): ...


class InvalidConcurrencyGroupStateError(ConcurrencyGroupError): ...


class ChildConcurrencyGroupDidNotExitError(ConcurrencyGroupError): ...


class ConcurrentShutdownError(ConcurrencyGroupError): ...


class AncestorConcurrentFailure(ConcurrencyGroupError):
    def __init__(self, ancestor_exception: Exception | None):
        self.ancestor_exception = ancestor_exception
        message = "An ancestor concurrency group failed."
        if ancestor_exception is not None:
            message += f" Ancestor exception: {ancestor_exception}"
        super().__init__(message)


class ConcurrencyExceptionGroup(ExceptionGroup):
    """
    Custom exception group subclass.

    The "main" exception is a convention that allows us to highlight the "original" exception in cases we know it.
    """

    def __new__(cls, message: str, exceptions: Sequence[Exception], main_exception: Exception | None = None):
        instance = super().__new__(cls, message, exceptions)
        return instance

    def __init__(self, message: str, exceptions: Sequence[Exception], main_exception: Exception | None = None):
        super().__init__(message, exceptions)
        self.main_exception = main_exception

    def __str__(self):
        base_str = super().__str__()
        if self.main_exception:
            return f"{base_str}\nMain exception: {self.main_exception}"
        return base_str

    def only_exception_is_instance_of(self, exception_class: type[Exception]) -> bool:
        """Check if the exception group is just a wrapper around a single exception of the given class."""
        return len(self.exceptions) == 1 and isinstance(self.exceptions[0], exception_class)

    def get_only_exception(
        self,
    ) -> Exception:
        if len(self.exceptions) != 1:
            raise ValueError("The exception group does not contain exactly one exception.")
        return self.exceptions[0]
