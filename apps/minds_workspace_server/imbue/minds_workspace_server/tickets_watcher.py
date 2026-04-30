"""Watch an agent's `.tickets/` directory for tk ticket changes.

Mirrors the pattern of session_watcher.AgentSessionWatcher: a background
thread combines watchdog filesystem events with mtime-based polling, and
emits parsed events through an `on_events(agent_id, events)` callback.

Each ticket file produces one event per state TRANSITION the watcher
actually observes. Stable event ids of `<ticket_id>-<status>` let the
frontend dedup across watcher restarts.

Live (watcher running through the whole ticket lifecycle): emits one
event per status change, so a typical ticket produces three events
(open at creation -> in_progress at `tk start` -> closed at `tk close`).

Replay (watcher started against a directory that already has tickets at
some non-`open` status): we cannot recover the historical timestamps of
transitions that already happened, so we emit a SINGLE event for the
current status with the file's mtime. The `created_at` field on the
event carries the ticket's frontmatter `created` value, so the frontend
still knows when the ticket existed even if it never saw the `open`
event. We do NOT synthesize fake `open` / `in_progress` events on
replay -- the event stream stays a faithful description of what was
observed, with the frontend filling in any missing lifecycle fields.
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
    (watchdog + polling fallback) and emits events for status changes.
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

        self._last_status_per_ticket: dict[str, str] = {}
        self._mtime_cache: dict[str, tuple[float, int]] = {}

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"tickets-watcher-{self._agent_id}")
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
        observed state changes -- the full cumulative history of
        transitions seen by this watcher. Safe to call repeatedly; the
        per-ticket last-status tracker keeps it idempotent for unchanged
        files. Used to seed initial state when an agent's chat is
        opened."""
        return self._scan()

    def _run(self) -> None:
        self._setup_watchers()
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break

            new_events = self._scan()
            if new_events:
                self._on_events(self._agent_id, new_events)

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _setup_watchers(self) -> None:
        # Watch the parent of .tickets/ (non-recursively) so we get a
        # wake-up if the .tickets/ directory itself is created mid-run.
        # If .tickets/ already exists, also watch it (non-recursively)
        # for sub-second latency on ticket file changes. recursive=True
        # on a project root would fire on every unrelated file change in
        # the agent's work tree, which is wasteful and can hit inotify
        # watch limits on busy checkouts.
        parent_dir = self._tickets_dir.parent
        if not parent_dir.exists():
            return
        try:
            observer = Observer()
            handler = _TicketsChangeHandler(self._wake_event)
            observer.schedule(handler, str(parent_dir), recursive=False)
            if self._tickets_dir.exists():
                observer.schedule(handler, str(self._tickets_dir), recursive=False)
            observer.start()
            self._observer = observer
        except OSError as e:
            # Watchdog start failure (typically inotify watch limit reached or
            # a permissions issue). Log at warning so operators see the
            # degradation; the polling-only fallback in _run keeps the
            # watcher functional, just with higher latency.
            logger.warning("Failed to start watchdog observer for tickets dir {}: {}", parent_dir, e)

    def _scan(self) -> list[dict[str, Any]]:
        """Scan the tickets directory and emit one event per OBSERVED
        state change. On first sighting of a new ticket, emit one event
        for its current status; on subsequent scans, emit one event each
        time the status moves forward (open -> in_progress -> closed).
        We never synthesize transitions we didn't observe."""
        if not self._tickets_dir.exists():
            return []

        new_events: list[dict[str, Any]] = []
        for md_file in sorted(self._tickets_dir.glob("*.md")):
            try:
                stat = md_file.stat()
            except OSError:
                continue

            mtime_key = (stat.st_mtime, stat.st_size)
            cached = self._mtime_cache.get(md_file.name)
            if cached == mtime_key:
                continue
            self._mtime_cache[md_file.name] = mtime_key

            state = parse_ticket_file(md_file)
            if state is None:
                continue

            previous_status = self._last_status_per_ticket.get(state.ticket_id)
            if previous_status == state.status:
                continue

            self._last_status_per_ticket[state.ticket_id] = state.status

            # First-sighting timestamp choice: an `open` ticket should
            # use its frontmatter `created` field (truthful, matches
            # when the file appeared); other statuses fall back to the
            # file's current mtime (the close time on replay; the
            # transition time live).
            if previous_status is None and state.status == "open":
                ts = state.created_at
            else:
                ts = _mtime_iso(md_file)

            new_events.append(self._make_event(state, ts))

        new_events.sort(key=lambda e: e["timestamp"])
        return new_events

    def _make_event(self, state: TicketState, ts: str) -> dict[str, Any]:
        return {
            "type": "task_event",
            "event_id": f"{state.ticket_id}-{state.status}",
            "timestamp": ts,
            "source": _SOURCE,
            "ticket_id": state.ticket_id,
            "title": state.title,
            "status": state.status,
            "created_at": state.created_at,
            "summary": state.summary if state.status == "closed" else None,
            "summary_at": state.summary_at if state.status == "closed" else None,
        }
