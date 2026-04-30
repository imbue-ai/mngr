"""Shared filesystem-watcher primitives for the agent state watchers.

The session watcher (Claude session JSONL files) and the tickets watcher
(tk `.tickets/` markdown files) both run the same pattern: a watchdog
Observer that wakes a polling loop on real filesystem changes. This
module hosts the shared primitive so each watcher does not have to
copy-paste it.
"""

from __future__ import annotations

import threading

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler

# Watchdog event types that do not represent actual content changes:
#   - "opened" / "closed" / "closed_no_write" fire on every file open
#     even when the contents weren't modified. We only want to wake the
#     watcher loop on real writes / creates / moves / deletes.
NON_CHANGE_EVENT_TYPES = frozenset({"opened", "closed", "closed_no_write"})

# How long the watcher's poll loop sleeps between scans when no
# watchdog event has woken it. Acts as a safety net if watchdog misses
# an event (e.g. inotify watch limits, network filesystems).
POLL_INTERVAL_SECONDS = 1.0


class WakeOnChangeHandler(FileSystemEventHandler):
    """Watchdog handler that wakes a watcher loop on real file changes.

    Filters out the non-content-change event types listed in
    `NON_CHANGE_EVENT_TYPES` so the watcher's wake event only triggers
    when something actually happened to a file's bytes.
    """

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()
