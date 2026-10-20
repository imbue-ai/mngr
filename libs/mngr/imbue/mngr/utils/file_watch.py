"""Event-driven wake-ups for local file tail loops.

The follow-mode tails in this package historically woke on a short fixed
interval to notice appended lines. This module lets those loops sleep on a
``threading.Event`` that a watchdog observer (inotify on Linux, FSEvents on
macOS) sets whenever anything in a watched directory changes, so the poll
interval only has to cover *missed* filesystem events (rare) instead of
providing delivery latency. Watching is strictly best-effort: when a watch
cannot be established, callers keep their original short-interval polling.
"""

import threading
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import PrivateAttr
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.api import ObservedWatch

from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.mutable_model import MutableModel

# Watchdog event types that carry no new content: "opened" and
# "closed_no_write" fire on every file open, including our own tail reads --
# waking on them would turn each read into a spurious self-wake -- and
# "closed" (a writer closing the file) adds nothing beyond the "modified"
# events its writes already fired.
NON_CONTENT_CHANGE_EVENT_TYPES: Final[frozenset[str]] = frozenset({"opened", "closed", "closed_no_write"})

# Fallback poll interval for a tail loop that is woken by a directory watch.
# Long enough to make idle wake-ups rare, short enough that an event missed by
# the watch backend (inotify watch limits, unusual filesystems) is only
# modestly late.
WATCHED_TAIL_FALLBACK_POLL_SECONDS: Final[float] = 10.0


class _WakeOnDirectoryChangeHandler(FileSystemEventHandler):
    """Sets every subscribed wake event on any content-changing filesystem event."""

    def __init__(self, wake_events: tuple[threading.Event, ...]) -> None:
        # Replaced wholesale (never mutated) by DirectoryWatchGroup under its
        # lock; read here on the observer thread without that lock, which is
        # safe because an attribute swap of an immutable tuple is atomic.
        # Taking the group lock here instead would deadlock against callers
        # that hold it across observer.schedule/unschedule.
        self.wake_events = wake_events

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in NON_CONTENT_CHANGE_EVENT_TYPES:
            return
        for wake_event in self.wake_events:
            wake_event.set()


class DirectoryWatchGroup(MutableModel):
    """One watchdog observer fanning directory changes out to per-directory wake events.

    ``watch`` is best-effort: when the observer cannot be started or a directory
    cannot be scheduled (inotify watch limits, a missing directory), it returns
    False and the caller keeps its polling fallback. The observer thread is a
    daemon, so an unstopped group never blocks process exit.

    A directory may be watched by multiple callers, each with its own wake
    event; ``unwatch`` removes only the given caller's subscription, and the
    underlying filesystem watch is released when the last subscription goes.
    """

    _observer: BaseObserver | None = PrivateAttr(default=None)
    _is_observer_broken: bool = PrivateAttr(default=False)
    _watch_by_dir: dict[str, ObservedWatch] = PrivateAttr(default_factory=dict)
    _handler_by_dir: dict[str, _WakeOnDirectoryChangeHandler] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def _get_or_start_observer_locked(self) -> BaseObserver | None:
        """Return the running observer, starting it lazily. None when it cannot start."""
        if self._is_observer_broken:
            return None
        if self._observer is not None:
            return self._observer
        observer = Observer()
        observer.daemon = True
        try:
            observer.start()
        except (OSError, RuntimeError) as e:
            logger.debug("Failed to start directory watch observer, tails fall back to polling: {}", e)
            self._is_observer_broken = True
            return None
        self._observer = observer
        return observer

    def watch(self, directory: Path, wake_event: threading.Event) -> bool:
        """Start setting ``wake_event`` on changes in ``directory``. Returns whether the watch is active.

        Subscribing an already-subscribed (directory, wake_event) pair is a no-op.
        """
        directory_key = str(directory)
        with self._lock:
            observer = self._get_or_start_observer_locked()
            if observer is None:
                return False
            existing_handler = self._handler_by_dir.get(directory_key)
            if existing_handler is not None:
                if not any(subscribed is wake_event for subscribed in existing_handler.wake_events):
                    existing_handler.wake_events = existing_handler.wake_events + (wake_event,)
                return True
            handler = _WakeOnDirectoryChangeHandler((wake_event,))
            try:
                watch = observer.schedule(handler, directory_key, recursive=False)
            except OSError as e:
                logger.debug("Failed to watch directory {}, tail falls back to polling: {}", directory_key, e)
                return False
            self._watch_by_dir[directory_key] = watch
            self._handler_by_dir[directory_key] = handler
            return True

    def unwatch(self, directory: Path, wake_event: threading.Event) -> None:
        """Remove ``wake_event``'s subscription to ``directory``. Idempotent."""
        directory_key = str(directory)
        with self._lock:
            handler = self._handler_by_dir.get(directory_key)
            if handler is None:
                return
            remaining_wake_events = tuple(
                subscribed for subscribed in handler.wake_events if subscribed is not wake_event
            )
            if remaining_wake_events:
                handler.wake_events = remaining_wake_events
                return
            self._handler_by_dir.pop(directory_key)
            watch = self._watch_by_dir.pop(directory_key, None)
            if watch is None or self._observer is None:
                return
            try:
                self._observer.unschedule(watch)
            except (OSError, KeyError) as e:
                logger.debug("Failed to unschedule watch for {}: {}", directory_key, e)

    def wake_all(self) -> None:
        """Set every registered wake event (used to unblock waiting tails at shutdown)."""
        with self._lock:
            wake_events = [
                wake_event for handler in self._handler_by_dir.values() for wake_event in handler.wake_events
            ]
        for wake_event in wake_events:
            wake_event.set()

    def stop(self) -> None:
        """Stop the observer thread. Idempotent; the group is unusable afterwards."""
        with self._lock:
            observer = self._observer
            self._observer = None
            self._is_observer_broken = True
            self._watch_by_dir.clear()
            self._handler_by_dir.clear()
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)


def _wait_for_source_then_set_target(source_event: threading.Event, target_event: threading.Event) -> None:
    source_event.wait()
    target_event.set()


def start_event_forwarder(source_event: threading.Event, target_event: threading.Event, name: str) -> None:
    """Set ``target_event`` as soon as ``source_event`` is set, via a parked daemon thread.

    Lets a loop that sleeps on one wake event also be woken by a stop event it
    does not own, without polling: the forwarder thread blocks on
    ``source_event.wait()`` (zero wake-ups) and fires once.
    """
    ObservableThread(
        target=_wait_for_source_then_set_target,
        args=(source_event, target_event),
        daemon=True,
        name=name,
    ).start()
