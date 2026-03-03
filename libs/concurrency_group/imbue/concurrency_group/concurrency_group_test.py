import contextlib
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from typing import Final

import pytest

from imbue.concurrency_group.concurrency_group import AncestorConcurrentFailure
from imbue.concurrency_group.concurrency_group import ChildConcurrencyGroupDidNotExitError
from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import ConcurrentShutdownError
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.concurrency_group import StrandTimedOutError
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.concurrency_group.test_utils import poll_until
from imbue.concurrency_group.test_utils import wait_interval
from imbue.concurrency_group.thread_utils import ObservableThread

TINY_SLEEP = 0.001
SMALL_SLEEP = 0.05


class _IntentionalTestError(Exception):
    """Raised intentionally by test threads to simulate failures."""


def _raise_intentional_error() -> None:
    raise _IntentionalTestError("intentional test failure")


# Process commands for tests: one that exits immediately, one that runs for a long time.
INSTANT_SUCCESS_COMMAND: Final[tuple[str, ...]] = ("true",)
LONG_RUNNING_COMMAND: Final[tuple[str, ...]] = ("sleep", "30")


def _sleep_and_return_1() -> int:
    """Thread target that takes a while to run, then returns 1."""
    wait_interval(0.3)
    return 1


def test_concurrency_group_shortly_waits_for_threads_to_finish() -> None:
    release_event = Event()

    def _wait_for_event_and_return_1() -> int:
        release_event.wait(timeout=5.0)
        return 1

    with ConcurrencyGroup(name="outer") as cg:
        thread1 = cg.start_new_thread(target=_wait_for_event_and_return_1)
        thread2 = cg.start_new_thread(target=_wait_for_event_and_return_1)
        assert thread1.is_alive()
        assert thread2.is_alive()
        release_event.set()
    assert not thread1.is_alive()
    assert not thread2.is_alive()


def test_concurrency_group_shortly_waits_for_processes_to_finish(tmp_path: Path) -> None:
    signal_file = tmp_path / "go"
    # Each process polls for the signal file, then exits.
    poll_cmd = f"while [ ! -f {signal_file} ]; do sleep 0.01; done"
    with ConcurrencyGroup(name="outer") as cg:
        process1 = cg.run_process_in_background(["bash", "-c", poll_cmd])
        process2 = cg.run_process_in_background(["bash", "-c", poll_cmd])
        # Processes are blocked waiting for signal file -- guaranteed still running.
        assert process1.poll() is None
        assert process2.poll() is None
        # Signal the processes to exit.
        signal_file.write_text("done")
    # ConcurrencyGroup.__exit__ waits for processes to finish.
    assert process1.poll() is not None
    assert process2.poll() is not None


def test_concurrency_group_supports_running_process_to_completion(tmp_path: Path) -> None:
    with ConcurrencyGroup(name="outer") as cg:
        process = cg.run_process_to_completion(INSTANT_SUCCESS_COMMAND)
    assert process.returncode == 0


def test_concurrency_group_supports_running_processes_with_on_output_callbacks(tmp_path: Path) -> None:
    calls: list[tuple[str, bool]] = []

    def callback(line: str, is_stdout: bool) -> None:
        calls.append((line, is_stdout))

    with ConcurrencyGroup(name="outer") as cg:
        process = cg.run_process_to_completion(["echo", "foo"], on_output=callback)
    assert process.returncode == 0
    assert len(calls) == 1
    assert calls[0] == ("foo\n", True)


def test_concurrency_group_supports_running_running_local_process_in_background(tmp_path: Path) -> None:
    with ConcurrencyGroup(name="outer") as cg:
        process = cg.run_process_in_background(INSTANT_SUCCESS_COMMAND)
        process.wait()
    assert process.poll() == 0


def test_concurrency_group_raises_timeout_when_not_finished_in_time() -> None:
    thread: ObservableThread | None = None
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer", exit_timeout_seconds=SMALL_SLEEP) as cg:
            thread = cg.start_new_thread(target=lambda: wait_interval(100.0))
    assert exception_info.value.only_exception_is_instance_of(StrandTimedOutError)
    assert thread is not None
    assert thread.is_alive()


def test_concurrency_group_does_not_raise_when_within_timeout() -> None:
    start_time = monotonic()
    with ConcurrencyGroup(name="outer", exit_timeout_seconds=SMALL_SLEEP) as cg:
        thread = cg.start_new_thread(target=lambda: None)
    end_time = monotonic()
    assert end_time - start_time < 0.1
    assert not thread.is_alive()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_failed_threads_raise_when_probed() -> None:
    i = 0
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg:
            thread = cg.start_new_thread(target=_raise_intentional_error)
            cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()
            i += 1
    assert exception_info.value.only_exception_is_instance_of(_IntentionalTestError)
    assert i == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_failed_threads_raise_when_exiting() -> None:
    i = 0
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg:
            thread = cg.start_new_thread(target=_raise_intentional_error)
            i += 1
    assert exception_info.value.only_exception_is_instance_of(_IntentionalTestError)
    assert i == 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_failed_threads_do_not_raise_when_suppressed() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        thread = cg.start_new_thread(target=_raise_intentional_error, suppressed_exceptions=(_IntentionalTestError,))
        cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_failed_threads_do_not_raise_when_explicitly_unchecked() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        thread = cg.start_new_thread(target=_raise_intentional_error, is_checked=False)
        cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()


def test_checked_failed_processes_raise_when_waited_for(tmp_path: Path) -> None:
    i = 0
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg:
            process = cg.run_process_in_background(["bash", "-c", "exit 1"], is_checked_by_group=True)
            process.wait()
            i += 1
        assert process.poll() == 1
    assert exception_info.value.only_exception_is_instance_of(ProcessError)
    assert i == 0


def test_checked_failed_processes_raise_when_probed(tmp_path: Path) -> None:
    i = 0
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg:
            process = cg.run_process_in_background(["bash", "-c", "exit 1"], is_checked_by_group=True)
            assert poll_until(lambda: process.poll() is not None, timeout=5.0)
            cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()
            i += 1
        assert process.poll() == 1
    assert exception_info.value.only_exception_is_instance_of(ProcessError)
    assert i == 0


def test_unchecked_failed_processes_do_not_raise(tmp_path: Path) -> None:
    i = 0
    with ConcurrencyGroup(name="outer") as cg:
        process = cg.run_process_in_background(["bash", "-c", "exit 1"], is_checked_by_group=False)
        process.wait()
        cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()
        i += 1
    assert process.poll() == 1
    assert i == 1


def test_unchecked_failed_foreground_process_setup_does_not_raise(tmp_path: Path) -> None:
    with ConcurrencyGroup(name="group") as cg:
        with contextlib.suppress(ProcessError):
            cg.run_process_to_completion(["does_not_exist_command"])


def test_probing_does_not_raise_when_no_failures_happened(tmp_path: Path) -> None:
    with ConcurrencyGroup(name="outer") as cg:
        process = cg.run_process_in_background(["bash", "-c", "exit 0"], is_checked_by_group=True)
        process.wait()
        thread = cg.start_new_thread(target=lambda: 1 + 1)
        thread.join()
        cg.raise_if_any_strands_or_ancestors_failed_or_is_shutting_down()


def test_do_not_allow_starting_new_strands_if_the_previous_failed(tmp_path: Path) -> None:
    process1: RunningProcess | None = None
    process2: RunningProcess | None = None
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg:
            process1 = cg.run_process_in_background(["bash", "-c", "exit 1"], is_checked_by_group=True)
            assert poll_until(lambda: process1.poll() is not None, timeout=5.0)
            process2 = cg.run_process_in_background(INSTANT_SUCCESS_COMMAND, is_checked_by_group=True)
    assert isinstance(exception_info.value.exceptions[0], ProcessError)
    assert process1 is not None
    assert process1.poll() == 1
    assert process2 is None


def test_all_failure_modes_get_combined(tmp_path: Path) -> None:
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer", exit_timeout_seconds=SMALL_SLEEP) as cg:
            process1 = cg.run_process_in_background(LONG_RUNNING_COMMAND, is_checked_by_group=True)
            process2 = cg.run_process_in_background(["bash", "-c", "exit 1"], is_checked_by_group=True)
            assert poll_until(lambda: process2.poll() is not None, timeout=5.0)
            raise _IntentionalTestError("intentional test failure")
    assert len(exception_info.value.exceptions) == 3
    assert any(isinstance(e, ProcessError) for e in exception_info.value.exceptions)
    assert any(isinstance(e, _IntentionalTestError) for e in exception_info.value.exceptions)
    assert any(isinstance(e, StrandTimedOutError) for e in exception_info.value.exceptions)


def test_nesting_in_the_same_thread_just_works() -> None:
    with ConcurrencyGroup(name="outer") as cg_outer:
        with cg_outer.make_concurrency_group(name="inner") as cg_inner:
            pass


def _create_nested_concurrency_group(
    concurrency_group: ConcurrencyGroup,
    closure: dict,
    thread_started_event: Event,
) -> None:
    with concurrency_group.make_concurrency_group(name="inner") as cg:
        cg.start_new_thread(target=lambda: closure.update({"i": _sleep_and_return_1()}))
        thread_started_event.set()


def test_nesting_across_threads_works_and_properly_waits() -> None:
    closure = {"i": 0}
    thread_started_event = Event()
    with ConcurrencyGroup(name="outer") as cg_outer:
        cg_outer.start_new_thread(
            target=_create_nested_concurrency_group, args=(cg_outer, closure, thread_started_event)
        )
        thread_started_event.wait(timeout=SMALL_SLEEP)
    assert closure["i"] == 1


def _create_nested_concurrency_group_that_expects_parent_failure(
    concurrency_group: ConcurrencyGroup,
    closure: dict,
    thread_started_event: Event,
) -> None:
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with concurrency_group.make_concurrency_group(name="inner") as cg:
            cg.start_new_thread(target=lambda: closure.update({"i": _sleep_and_return_1()}))
            thread_started_event.set()
    assert exception_info.value.only_exception_is_instance_of(AncestorConcurrentFailure)


def test_nesting_across_threads_raises_timeout_when_child_group_does_not_finish_in_time() -> None:
    closure = {"i": 0}
    thread_started_event = Event()
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer", exit_timeout_seconds=TINY_SLEEP) as cg_outer:
            cg_outer.start_new_thread(
                target=_create_nested_concurrency_group_that_expects_parent_failure,
                args=(cg_outer, closure, thread_started_event),
            )
            thread_started_event.wait(timeout=SMALL_SLEEP)
    assert any(
        isinstance(exception, ChildConcurrencyGroupDidNotExitError) for exception in exception_info.value.exceptions
    )
    assert closure["i"] == 0


def _create_nested_failing_concurrency_group(concurrency_group: ConcurrencyGroup, thread_started_event: Event) -> None:
    with concurrency_group.make_concurrency_group(name="inner") as cg:
        cg.start_new_thread(target=_raise_intentional_error)
        thread_started_event.set()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_error_from_nested_group_in_another_thread_gets_properly_propagated() -> None:
    thread_started_event = Event()
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer") as cg_outer:
            cg_outer.start_new_thread(
                target=_create_nested_failing_concurrency_group, args=(cg_outer, thread_started_event)
            )
            thread_started_event.wait(timeout=SMALL_SLEEP)
    assert len(exception_info.value.exceptions) == 1
    assert isinstance(exception_info.value.exceptions[0], ConcurrencyExceptionGroup)
    assert len(exception_info.value.exceptions[0].exceptions) == 1
    assert isinstance(exception_info.value.exceptions[0].exceptions[0], _IntentionalTestError)


def _create_two_nested_concurrency_groups_that_expect_parent_failure(
    concurrency_group: ConcurrencyGroup, closure: dict, setup_done_event: Event
) -> None:
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with concurrency_group.make_concurrency_group(name="middle") as cg_middle:
            try:
                with cg_middle.make_concurrency_group(name="inner") as cg_inner:
                    # Thread must outlast the outer CG's TINY_SLEEP exit timeout (to trigger
                    # the timeout error) but eventually finish (to update the closure).
                    thread = cg_inner.start_new_thread(target=lambda: closure.update({"i": _sleep_and_return_1()}))
                    setup_done_event.set()
                    thread.join()
            except ConcurrencyExceptionGroup as exception_info:
                assert len(exception_info.exceptions) == 1
                ancestor_failure = exception_info.exceptions[0]
                assert isinstance(ancestor_failure, AncestorConcurrentFailure)
                ancestor_exception = ancestor_failure.ancestor_exception
                assert isinstance(ancestor_exception, ConcurrencyExceptionGroup)
                assert len(ancestor_exception.exceptions) == 2
                assert any(isinstance(e, StrandTimedOutError) for e in ancestor_exception.exceptions)
                assert any(isinstance(e, ChildConcurrencyGroupDidNotExitError) for e in ancestor_exception.exceptions)
                closure["i"] += 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_parent_failures_propagate_recursively() -> None:
    closure: dict[str, Any] = {"i": 0}
    setup_done_event = Event()
    outer_thread: ObservableThread | None = None
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with ConcurrencyGroup(name="outer", exit_timeout_seconds=TINY_SLEEP) as cg_outer:
            outer_thread = cg_outer.start_new_thread(
                target=_create_two_nested_concurrency_groups_that_expect_parent_failure,
                args=(cg_outer, closure, setup_done_event),
            )
            setup_done_event.wait(timeout=5.0)
    assert outer_thread is not None
    outer_thread.join()
    assert closure["i"] == 2


def test_exhausted_concurrency_group_cannot_be_entered_again() -> None:
    cg = ConcurrencyGroup(name="outer")
    with cg:
        cg.start_new_thread(target=lambda: 1)
    with pytest.raises(InvalidConcurrencyGroupStateError):
        with cg:
            pass


def test_exhausted_concurrency_group_cannot_start_threads() -> None:
    cg = ConcurrencyGroup(name="outer")
    with cg:
        cg.start_new_thread(target=lambda: 1)
    with pytest.raises(InvalidConcurrencyGroupStateError):
        cg.start_new_thread(target=lambda: 1)


def test_exhausted_concurrency_group_cannot_make_nested_groups() -> None:
    cg = ConcurrencyGroup(name="outer")
    with cg:
        cg.start_new_thread(target=lambda: 1)
    with pytest.raises(InvalidConcurrencyGroupStateError):
        cg_nested = cg.make_concurrency_group(name="inner")


# Shutdown-related tests


def _create_nested_concurrency_group_and_run_process(
    concurrency_group: ConcurrencyGroup,
    closure: dict,
    tmp_path: Path,
    process_started_event: Event,
) -> None:
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with concurrency_group.make_concurrency_group(name="inner") as cg:
            process = cg.run_process_in_background(LONG_RUNNING_COMMAND, is_checked_by_group=True)
            process_started_event.set()
            process.wait()
            closure["i"] += 1
    assert exception_info.value.only_exception_is_instance_of(ProcessError)
    closure["i"] += 10


def test_shutdown_propagates_to_children_and_kills_processes(tmp_path: Path) -> None:
    closure = {"i": 0}
    process_started_event = Event()
    with ConcurrencyGroup(name="outer") as cg:
        thread = cg.start_new_thread(
            target=_create_nested_concurrency_group_and_run_process,
            args=(cg, closure, tmp_path, process_started_event),
        )
        process_started_event.wait(timeout=SMALL_SLEEP)
        cg.shutdown()
    assert closure["i"] == 10


def _create_nested_concurrency_group_and_run_process_while_shutting_down(
    concurrency_group: ConcurrencyGroup,
    tmp_path: Path,
    closure: dict,
    process_started_event: Event,
) -> None:
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info:
        with concurrency_group.make_concurrency_group(name="inner") as cg:
            process_started_event.set()
            wait_interval(SMALL_SLEEP)
            closure["i"] += 1
            process = cg.run_process_in_background(LONG_RUNNING_COMMAND, is_checked_by_group=True)
            process.wait()
            closure["i"] += 1
        assert exception_info.value.only_exception_is_instance_of(ConcurrentShutdownError)


def test_new_resources_cannot_be_created_when_shutting_down(tmp_path: Path) -> None:
    closure = {"i": 0}
    process_started_event = Event()
    with ConcurrencyGroup(name="outer") as cg:
        thread = cg.start_new_thread(
            target=_create_nested_concurrency_group_and_run_process_while_shutting_down,
            args=(cg, tmp_path, closure, process_started_event),
        )
        process_started_event.wait(timeout=SMALL_SLEEP)
        cg.shutdown()
    assert closure["i"] == 1


def test_threads_get_cleaned_up() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        for _ in range(20):
            cg.start_new_thread(target=lambda: 1).join()
        cg.start_new_thread(target=lambda: 1)
        assert len(cg._threads) <= 1


def test_processes_get_cleaned_up() -> None:
    with ConcurrencyGroup(name="outer") as cg:
        for _ in range(20):
            cg.run_process_in_background(["echo", "foo"]).wait()
        cg.run_process_in_background(["echo", "foo"])
        assert len(cg._processes) <= 1


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_new_resources_cannot_be_created_when_ancestor_has_failed_strands() -> None:
    exception_info_thread: Any = None
    with pytest.raises(ConcurrencyExceptionGroup) as exception_info_outer:
        with ConcurrencyGroup(name="outer") as cg_outer:
            with pytest.raises(ConcurrencyExceptionGroup) as exception_info_inner:
                with cg_outer.make_concurrency_group(name="inner") as cg_inner:
                    outer_failed_thread = cg_outer.start_new_thread(target=_raise_intentional_error)
                    with pytest.raises(_IntentionalTestError):
                        outer_failed_thread.join()
                    with pytest.raises(ConcurrencyExceptionGroup):
                        outer_successful_thread = cg_outer.start_new_thread(target=lambda: 1)
                    with pytest.raises(ConcurrencyExceptionGroup) as exception_info_thread:
                        inner_thread = cg_inner.start_new_thread(target=lambda: 1)
    assert exception_info_thread.value.only_exception_is_instance_of(AncestorConcurrentFailure)
