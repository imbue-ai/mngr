import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.process_exit_watch import ProcessExitWatch


@contextmanager
def _process_exit_watch() -> Iterator[ProcessExitWatch]:
    with ConcurrencyGroup(name=f"process-exit-watch-test-{uuid4().hex}") as concurrency_group:
        watch = ProcessExitWatch(concurrency_group=concurrency_group, thread_name=f"exit-watch-{uuid4().hex}")
        try:
            yield watch
        finally:
            watch.close()


def _end(process: subprocess.Popen[bytes]) -> None:
    process.kill()
    process.wait()


@contextmanager
def _sleepers(count: int) -> Iterator[list[subprocess.Popen[bytes]]]:
    processes = [subprocess.Popen(["sleep", "48213"]) for _ in range(count)]
    try:
        yield processes
    finally:
        for process in processes:
            _end(process)


def test_watch_calls_back_once_when_the_watched_process_exits() -> None:
    exited: list[str] = []
    with _process_exit_watch() as watch, _sleepers(1) as (process,):
        assert watch.watch("agent-a", process.pid, lambda: exited.append("agent-a"))

        _end(process)

        wait_for(lambda: exited == ["agent-a"], error_message="the exit was not reported")
        assert watch.watched_pid("agent-a") is None


def test_unwatched_process_exit_is_not_reported() -> None:
    exited: list[str] = []
    with _process_exit_watch() as watch, _sleepers(2) as (unwatched, sentinel):
        watch.watch("unwatched", unwatched.pid, lambda: exited.append("unwatched"))
        watch.watch("sentinel", sentinel.pid, lambda: exited.append("sentinel"))
        watch.unwatch("unwatched")

        _end(unwatched)
        _end(sentinel)

        wait_for(lambda: "sentinel" in exited, error_message="the sentinel's exit was not reported")
        assert exited == ["sentinel"]


def test_watching_a_key_with_another_pid_follows_the_new_process() -> None:
    exited: list[str] = []
    with _process_exit_watch() as watch, _sleepers(2) as (first, second):
        watch.watch("agent-a", first.pid, lambda: exited.append("first"))
        watch.watch("agent-a", second.pid, lambda: exited.append("second"))
        assert watch.watched_pid("agent-a") == second.pid

        _end(first)
        _end(second)

        wait_for(lambda: "second" in exited, error_message="the replacement's exit was not reported")
        assert exited == ["second"]


def test_watch_reports_a_process_that_is_already_gone() -> None:
    with _process_exit_watch() as watch, _sleepers(1) as (process,):
        _end(process)

        assert watch.watch("agent-a", process.pid, lambda: None) is False
        assert watch.watched_pid("agent-a") is None


def test_many_watched_processes_share_one_thread_and_each_exit_is_reported() -> None:
    exited: list[int] = []
    exited_lock = threading.Lock()
    with _process_exit_watch() as watch, _sleepers(12) as processes:
        threads_before = set(threading.enumerate())
        for process in processes:
            pid = process.pid
            watch.watch(f"agent-{pid}", pid, lambda pid=pid: _append_locked(exited, exited_lock, pid))

        new_threads = set(threading.enumerate()) - threads_before
        assert [thread.name for thread in new_threads] == [watch.thread_name]

        for process in processes:
            _end(process)
        wait_for(lambda: len(exited) == len(processes), error_message="not every exit was reported")
        assert sorted(exited) == sorted(process.pid for process in processes)


def test_close_stops_the_watch_thread_and_refuses_later_watches() -> None:
    with _process_exit_watch() as watch, _sleepers(1) as (process,):
        watch.watch("agent-a", process.pid, lambda: None)
        watch_threads = [thread for thread in threading.enumerate() if thread.name == watch.thread_name]
        assert len(watch_threads) == 1

        watch.close()

        assert not watch_threads[0].is_alive()
        assert watch.watch("agent-b", process.pid, lambda: None) is False


def _append_locked(values: list[int], lock: threading.Lock, value: int) -> None:
    with lock:
        values.append(value)
