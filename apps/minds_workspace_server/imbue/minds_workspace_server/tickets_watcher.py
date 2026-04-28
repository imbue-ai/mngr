"""Watch an agent's `.tickets/` directory for tk ticket changes.

Mirrors the pattern of session_watcher.AgentSessionWatcher: a background
thread combines watchdog filesystem events with mtime-based polling, and
emits parsed events through an `on_events(agent_id, events)` callback.

Each ticket file produces up to three task events over its lifetime, one
per status transition: open -> in_progress -> closed. Event IDs are
derived from the ticket id + status so they're stable across watcher
restarts (the frontend dedups by event_id).

Live (watcher running when transition happens): timestamps come from the
file's mtime at observation time, which closely matches when the agent
ran `tk start` / `tk close`.

Replay (watcher started against a directory that already has closed
tickets): we cannot recover the historical mtime of a transition that
already happened, so the in_progress timestamp falls back to the current
mtime (i.e. close time). Frontend rendering tolerates the resulting
zero-width "active window" gracefully.
"""

from __future__ import annotations

import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger as _loguru_logger
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from imbue.minds_workspace_server.tickets_parser import TicketState
from imbue.minds_workspace_server.tickets_parser import parse_ticket_file

logger = _loguru_logger

_NON_CHANGE_EVENT_TYPES = frozenset({"opened", "closed", "closed_no_write"})

_POLL_INTERVAL_SECONDS = 1.0
_SOURCE = "tk"


class _TicketsChangeHandler(FileSystemEventHandler):
    """Wakes the watcher loop on real filesystem changes."""

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


def _mtime_iso(path: Path) -> str:
    """File mtime formatted as a UTC ISO-8601 timestamp (matches tk's own
    `created:` field format)."""
    mtime = path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgentTicketsWatcher:
    """Watches an agent's `.tickets/` directory and emits task events.

    The directory is allowed to not exist yet; the watcher just stays
    silent until it's created. Once present, it observes changes
    (watchdog + polling fallback) and emits new events whenever a ticket
    transitions to a status it hasn't been observed in before.
    """

    def __init__(
        self,
        agent_id: str,
        tickets_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._tickets_dir = tickets_dir
        self._on_events = on_events

        self._existing_event_ids: set[str] = set()
        self._mtime_cache: dict[str, tuple[float, int]] = {}

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"tickets-watcher-{self._agent_id}"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get_all_events(self) -> list[dict[str, Any]]:
        """Scan the tickets directory and return every event implied by
        its current contents -- the full cumulative history, not just
        what changed since the last call. Used to seed initial state
        when an agent's chat is opened. Safe to call repeatedly."""
        return self._collect_events(emit_only_new=False)

    def _run(self) -> None:
        self._setup_watchers()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break

            new_events = self._collect_events(emit_only_new=True)
            if new_events:
                self._on_events(self._agent_id, new_events)

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _setup_watchers(self) -> None:
        # Watch the parent of .tickets/ so we still get a wake-up if the
        # directory is created mid-run.
        watch_dir = self._tickets_dir.parent
        if not watch_dir.exists():
            return
        try:
            observer = Observer()
            handler = _TicketsChangeHandler(self._wake_event)
            observer.schedule(handler, str(watch_dir), recursive=True)
            observer.start()
            self._observer = observer
        except OSError:
            logger.debug("Failed to start watchdog observer for tickets dir: %s", watch_dir)

    def _collect_events(self, *, emit_only_new: bool) -> list[dict[str, Any]]:
        """Scan and produce events for every ticket file currently on disk.

        emit_only_new=True (the live polling path) skips files whose mtime
        hasn't changed and filters out events whose event_id was already
        emitted. emit_only_new=False (the initial-load path) returns the
        cumulative event list reflecting current disk state, while still
        keeping the dedup set in sync so the live path doesn't double-emit
        afterwards.
        """
        if not self._tickets_dir.exists():
            return []

        results: list[dict[str, Any]] = []
        for md_file in sorted(self._tickets_dir.glob("*.md")):
            try:
                stat = md_file.stat()
            except OSError:
                continue

            mtime_key = (stat.st_mtime, stat.st_size)
            cached = self._mtime_cache.get(md_file.name)
            if emit_only_new and cached == mtime_key:
                continue
            self._mtime_cache[md_file.name] = mtime_key

            state = parse_ticket_file(md_file)
            if state is None:
                continue

            mtime_ts = _mtime_iso(md_file)
            for event in self._events_for_state(state, mtime_ts):
                if emit_only_new and event["event_id"] in self._existing_event_ids:
                    continue
                self._existing_event_ids.add(event["event_id"])
                results.append(event)

        results.sort(key=lambda e: e["timestamp"])
        return results

    def _events_for_state(self, state: TicketState, mtime_ts: str) -> list[dict[str, Any]]:
        """Generate the events implied by a ticket's current status. Up to
        three: open, in_progress, closed. The frontend dedups by event_id
        so re-emitting on a no-op scan is harmless."""
        events: list[dict[str, Any]] = [self._make_event(state, "open", state.created_at)]
        if state.status in {"in_progress", "closed"}:
            events.append(self._make_event(state, "in_progress", mtime_ts))
        if state.status == "closed":
            events.append(self._make_event(state, "closed", mtime_ts))
        return events

    def _make_event(self, state: TicketState, status: str, ts: str) -> dict[str, Any]:
        return {
            "type": "task_event",
            "event_id": f"{state.ticket_id}-{status}",
            "timestamp": ts,
            "source": _SOURCE,
            "ticket_id": state.ticket_id,
            "title": state.title,
            "status": status,
            "created_at": state.created_at,
            "summary": state.summary if status == "closed" else None,
            "summary_at": state.summary_at if status == "closed" else None,
        }
