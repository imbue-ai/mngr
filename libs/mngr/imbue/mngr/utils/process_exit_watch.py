import os
import select
import sys
import threading
from collections.abc import Callable
from collections.abc import Mapping
from typing import Final

import psutil
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

# How often a process the kernel cannot report the exit of is checked instead.
_POLLED_EXIT_CHECK_SECONDS: Final[float] = 3.0
_THREAD_JOIN_TIMEOUT_SECONDS: Final[float] = 5.0
_WAKE_PIPE_READ_BYTES: Final[int] = 4096


class _WatchedProcess(FrozenModel):
    """One watched process and what to call when it exits."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    process: psutil.Process = Field(description="The process, pinned by its creation time against pid reuse")
    on_exit: Callable[[], None] = Field(description="Called once, on the watch thread, when the process exits")


def _has_exited(process: psutil.Process) -> bool:
    if not process.is_running():
        return True
    try:
        return process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_with_pidfds(processes_by_key: Mapping[str, psutil.Process], wake_read_fd: int) -> set[str]:
    """Linux: poll a pidfd per process, readable once it exits, beside the wake pipe."""
    assert sys.platform == "linux"
    exited_keys: set[str] = set()
    polled_keys: list[str] = []
    key_by_pidfd: dict[int, str] = {}
    try:
        for key, process in processes_by_key.items():
            try:
                pidfd = os.pidfd_open(process.pid)
            except ProcessLookupError:
                exited_keys.add(key)
                continue
            except OSError as e:
                logger.trace("Could not open a pidfd for pid {} (polling it instead): {}", process.pid, e)
                polled_keys.append(key)
                continue
            key_by_pidfd[pidfd] = key
            # The pidfd pins whichever process owns the pid now; if that is not the process
            # the watch began on, that one exited and its pid was reused.
            if not process.is_running():
                exited_keys.add(key)
        if exited_keys:
            return exited_keys
        poller = select.poll()
        poller.register(wake_read_fd, select.POLLIN)
        for pidfd in key_by_pidfd:
            poller.register(pidfd, select.POLLIN)
        timeout_milliseconds = _POLLED_EXIT_CHECK_SECONDS * 1000 if polled_keys else None
        for fd, _event_mask in poller.poll(timeout_milliseconds):
            if fd in key_by_pidfd:
                exited_keys.add(key_by_pidfd[fd])
    finally:
        for pidfd in key_by_pidfd:
            os.close(pidfd)
    exited_keys.update(key for key in polled_keys if _has_exited(processes_by_key[key]))
    return exited_keys


def _wait_with_kqueue(processes_by_key: Mapping[str, psutil.Process], wake_read_fd: int) -> set[str]:
    """macOS: block in one kqueue holding an exit filter per process and a read filter on the wake pipe."""
    assert sys.platform == "darwin"
    exited_keys: set[str] = set()
    polled_keys: list[str] = []
    keys_by_pid: dict[int, list[str]] = {}
    kqueue = select.kqueue()
    try:
        kqueue.control([select.kevent(wake_read_fd, filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD)], 0, 0)
        for key, process in processes_by_key.items():
            exit_filter = select.kevent(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                kqueue.control([exit_filter], 0, 0)
            except ProcessLookupError:
                exited_keys.add(key)
                continue
            except OSError as e:
                logger.trace("Could not watch pid {} with kqueue (polling it instead): {}", process.pid, e)
                polled_keys.append(key)
                continue
            keys_by_pid.setdefault(process.pid, []).append(key)
            # The filter names a pid, not a process: if the pid belongs to another process
            # now, the one the watch began on already exited.
            if not process.is_running():
                exited_keys.add(key)
        if exited_keys:
            return exited_keys
        timeout_seconds = _POLLED_EXIT_CHECK_SECONDS if polled_keys else None
        for event in kqueue.control(None, len(keys_by_pid) + 1, timeout_seconds):
            if event.filter == select.KQ_FILTER_PROC:
                exited_keys.update(keys_by_pid.get(event.ident, ()))
    finally:
        kqueue.close()
    exited_keys.update(key for key in polled_keys if _has_exited(processes_by_key[key]))
    return exited_keys


def _wait_by_polling(processes_by_key: Mapping[str, psutil.Process], wake_read_fd: int) -> set[str]:
    """Anywhere else: check every process, then wait on the wake pipe until the next check."""
    exited_keys = {key for key, process in processes_by_key.items() if _has_exited(process)}
    if exited_keys:
        return exited_keys
    select.select([wake_read_fd], [], [], _POLLED_EXIT_CHECK_SECONDS if processes_by_key else None)
    return {key for key, process in processes_by_key.items() if _has_exited(process)}


def _wait_for_any_exit(processes_by_key: Mapping[str, psutil.Process], wake_read_fd: int) -> set[str]:
    """Block until a watched process exits or the wake pipe is readable; the keys of the processes found exited."""
    if sys.platform == "linux":
        return _wait_with_pidfds(processes_by_key, wake_read_fd)
    elif sys.platform == "darwin":
        return _wait_with_kqueue(processes_by_key, wake_read_fd)
    else:
        return _wait_by_polling(processes_by_key, wake_read_fd)


class ProcessExitWatch(MutableModel):
    """Calls back when a watched process exits, waiting on every watched process from one thread.

    The thread starts with the first watch and blocks in the kernel (pidfds on Linux, a
    kqueue on macOS) with no timer. Each change to the watched set wakes it to wait on
    the new set.
    """

    concurrency_group: ConcurrencyGroup = Field(frozen=True, description="The group that owns the watch thread")
    thread_name: str = Field(frozen=True, description="The name of the watch thread")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _watched_by_key: dict[str, _WatchedProcess] = PrivateAttr(default_factory=dict)
    _wake_read_fd: int | None = PrivateAttr(default=None)
    _wake_write_fd: int | None = PrivateAttr(default=None)
    _thread: ObservableThread | None = PrivateAttr(default=None)
    _is_closed: bool = PrivateAttr(default=False)

    def watch(self, key: str, pid: int, on_exit: Callable[[], None]) -> bool:
        """Watch ``pid`` under ``key``, replacing a different pid watched there.

        Returns False, watching nothing under ``key``, when no such process runs or the watch is closed.
        ``on_exit`` is called once, on the watch thread, when the process exits, unless the
        key is unwatched or watched with another pid first, or the watch is closed. Watching
        the pid a key already watches keeps the first callback.
        """
        with self._lock:
            if self._is_closed:
                return False
            existing = self._watched_by_key.get(key)
            if existing is not None and existing.process.pid == pid:
                self._ensure_thread_locked()
                return True
            try:
                process = psutil.Process(pid)
            except psutil.NoSuchProcess:
                if self._watched_by_key.pop(key, None) is not None:
                    self._wake_locked()
                return False
            self._watched_by_key[key] = _WatchedProcess(process=process, on_exit=on_exit)
            self._ensure_thread_locked()
            self._wake_locked()
            return True

    def unwatch(self, key: str) -> None:
        with self._lock:
            if self._watched_by_key.pop(key, None) is not None:
                self._wake_locked()

    def watched_pid(self, key: str) -> int | None:
        with self._lock:
            watched = self._watched_by_key.get(key)
        return None if watched is None else watched.process.pid

    def close(self) -> None:
        """Stop watching everything and join the watch thread. Idempotent."""
        with self._lock:
            self._is_closed = True
            self._watched_by_key.clear()
            self._wake_locked()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                # The pipe stays open rather than close under a thread still waiting on it.
                logger.warning("The process exit watch thread {} did not stop", self.thread_name)
                return
        with self._lock:
            wake_fds = (self._wake_read_fd, self._wake_write_fd)
            self._wake_read_fd = None
            self._wake_write_fd = None
        for fd in wake_fds:
            if fd is not None:
                os.close(fd)

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._wake_read_fd is None:
            self._wake_read_fd, self._wake_write_fd = os.pipe()
            os.set_blocking(self._wake_read_fd, False)
            os.set_blocking(self._wake_write_fd, False)
        self._thread = self.concurrency_group.start_new_thread(
            target=self._wait_for_exits,
            args=(self._wake_read_fd,),
            name=self.thread_name,
            daemon=True,
            # A failed watch thread must not fail the group's other work: the next watch
            # starts another, and until then the owner's own polling covers any exit.
            is_checked=False,
            on_failure=self._on_thread_failure,
        )

    def _wake_locked(self) -> None:
        if self._wake_write_fd is None:
            return
        try:
            os.write(self._wake_write_fd, b"\0")
        except BlockingIOError:
            # A full pipe already holds a pending wake.
            pass

    def _on_thread_failure(self, e: BaseException) -> None:
        logger.opt(exception=e).warning("The process exit watch thread {} failed", self.thread_name)

    def _read_watched_after_draining_wakes(self, wake_read_fd: int) -> dict[str, _WatchedProcess] | None:
        """The watched set, or None once the watch is closed.

        The pipe is drained before the set is read, so a change made after the read leaves
        a byte in it and the next wait returns at once.
        """
        _drain_pipe(wake_read_fd)
        with self._lock:
            return None if self._is_closed else dict(self._watched_by_key)

    def _wait_for_exits(self, wake_read_fd: int) -> None:
        watched_by_key = self._read_watched_after_draining_wakes(wake_read_fd)
        while watched_by_key is not None:
            exited_keys = _wait_for_any_exit(
                {key: watched.process for key, watched in watched_by_key.items()}, wake_read_fd
            )
            for key in exited_keys:
                watched = watched_by_key[key]
                with self._lock:
                    is_still_watched = not self._is_closed and self._watched_by_key.get(key) is watched
                    if is_still_watched:
                        del self._watched_by_key[key]
                if is_still_watched:
                    watched.on_exit()
            watched_by_key = self._read_watched_after_draining_wakes(wake_read_fd)


def _drain_pipe(read_fd: int) -> None:
    is_drained = False
    while not is_drained:
        try:
            is_drained = len(os.read(read_fd, _WAKE_PIPE_READ_BYTES)) < _WAKE_PIPE_READ_BYTES
        except BlockingIOError:
            is_drained = True
