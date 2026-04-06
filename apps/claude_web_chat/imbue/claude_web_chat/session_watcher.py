"""Watch raw Claude session JSONL files for new events.

Uses watchdog for low-latency filesystem change detection with mtime-based
polling as a safety net fallback, following the pattern from watcher_common.py
in mngr_recursive.
"""

from __future__ import annotations

import json
from loguru import logger as _loguru_logger
import os
import threading
import time
from pathlib import Path
from typing import Any
from typing import Callable

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from imbue.claude_web_chat.session_parser import parse_session_lines

logger = _loguru_logger

_NON_CHANGE_EVENT_TYPES = frozenset({"opened", "closed", "closed_no_write"})

_POLL_INTERVAL_SECONDS = 1.0
_BRIEF_WAIT_SECONDS = 0.5


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that wakes the watcher on actual file changes."""

    def __init__(self, wake_event: threading.Event) -> None:
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


class SessionFileState:
    """Tracks reading state for a single session JSONL file."""

    def __init__(self, session_id: str, file_path: Path) -> None:
        self.session_id = session_id
        self.file_path = file_path
        self.byte_offset: int = 0
        self.last_mtime: float = 0.0
        self.last_size: int = 0


class AgentSessionWatcher:
    """Watches all session files for a single mngr agent and emits parsed events."""

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        claude_config_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._agent_state_dir = agent_state_dir
        self._claude_config_dir = claude_config_dir
        self._on_events = on_events

        self._session_states: dict[str, SessionFileState] = {}
        self._known_session_ids: list[str] = []
        self._tool_name_by_call_id: dict[str, str] = {}
        self._existing_event_ids: set[str] = set()

        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._observer: Observer | None = None
        self._thread: threading.Thread | None = None
        self._mtime_cache: dict[str, tuple[float, int]] = {}

    def start(self) -> None:
        """Start watching session files in a background thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"watcher-{self._agent_id}")
        self._thread.start()

    def stop(self) -> None:
        """Stop watching."""
        self._stop_event.set()
        self._wake_event.set()
        if self._observer is not None:
            self._observer.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get_all_events(self, tail: int | None = None) -> list[dict[str, Any]]:
        """Read all session files and return parsed events.

        This is used for the initial load. Reads all known sessions from scratch.
        """
        self._discover_sessions()
        all_lines: list[str] = []
        for session_state in self._session_states.values():
            if not session_state.file_path.exists():
                continue
            try:
                content = session_state.file_path.read_text()
                all_lines.extend(content.splitlines())
            except OSError:
                logger.debug("Failed to read session file: %s", session_state.file_path)

        # Parse without deduplication for initial load
        tool_names: dict[str, str] = {}
        events = parse_session_lines(all_lines, existing_event_ids=None, tool_name_by_call_id=tool_names)
        # Update shared state
        self._tool_name_by_call_id.update(tool_names)
        for event in events:
            self._existing_event_ids.add(event["event_id"])

        if tail is not None and len(events) > tail:
            return events[-tail:]
        return events

    def get_backfill_events(self, before_event_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Get events before a given event_id for backfill pagination."""
        self._discover_sessions()
        all_lines: list[str] = []
        for session_state in self._session_states.values():
            if not session_state.file_path.exists():
                continue
            try:
                content = session_state.file_path.read_text()
                all_lines.extend(content.splitlines())
            except OSError:
                continue

        tool_names: dict[str, str] = {}
        all_events = parse_session_lines(all_lines, existing_event_ids=None, tool_name_by_call_id=tool_names)

        # Find the index of the before_event_id
        target_idx = -1
        for i, event in enumerate(all_events):
            if event["event_id"] == before_event_id:
                target_idx = i
                break

        if target_idx <= 0:
            return []

        start_idx = max(0, target_idx - limit)
        return all_events[start_idx:target_idx]

    def _run(self) -> None:
        """Main watcher loop."""
        self._discover_sessions()
        self._setup_watchers()
        self._read_initial_offsets()

        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            self._wake_event.clear()

            if self._stop_event.is_set():
                break

            self._discover_sessions()
            self._poll_for_changes()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)

    def _discover_sessions(self) -> None:
        """Read claude_session_id_history to find all session IDs."""
        history_file = self._agent_state_dir / "claude_session_id_history"
        if not history_file.exists():
            return

        try:
            lines = history_file.read_text().splitlines()
        except OSError:
            return

        for line in lines:
            parts = line.strip().split()
            if not parts:
                continue
            session_id = parts[0]
            if session_id in self._session_states:
                continue

            # Try to find the session file
            file_path = self._find_session_file(session_id)
            if file_path is None:
                # Brief wait then try again
                time.sleep(_BRIEF_WAIT_SECONDS)
                file_path = self._find_session_file(session_id)
                if file_path is None:
                    logger.debug("Session file not found for %s, will retry on next cycle", session_id)
                    continue

            self._session_states[session_id] = SessionFileState(session_id, file_path)
            self._known_session_ids.append(session_id)

            # Set up watchdog for the new file
            if self._observer is not None:
                parent_dir = str(file_path.parent)
                try:
                    self._observer.schedule(_ChangeHandler(self._wake_event), parent_dir, recursive=False)
                except OSError:
                    logger.debug("Failed to schedule watchdog for %s", parent_dir)

    def _find_session_file(self, session_id: str) -> Path | None:
        """Search for a session JSONL file under the Claude projects directory."""
        projects_dir = self._claude_config_dir / "projects"
        if not projects_dir.exists():
            return None

        # Walk the projects directory looking for the session file
        target_name = f"{session_id}.jsonl"
        for root, _dirs, files in os.walk(str(projects_dir)):
            if target_name in files:
                return Path(root) / target_name
        return None

    def _setup_watchers(self) -> None:
        """Set up watchdog observers for known session file directories."""
        watched_dirs: set[str] = set()
        for state in self._session_states.values():
            if state.file_path.exists():
                watched_dirs.add(str(state.file_path.parent))

        # Also watch the history file's directory
        history_file = self._agent_state_dir / "claude_session_id_history"
        if history_file.parent.exists():
            watched_dirs.add(str(history_file.parent))

        if not watched_dirs:
            return

        try:
            observer = Observer()
            handler = _ChangeHandler(self._wake_event)
            for dir_path in watched_dirs:
                observer.schedule(handler, dir_path, recursive=False)
            observer.start()
            self._observer = observer
        except OSError:
            logger.debug("Failed to start watchdog observer, falling back to polling only")

    def _read_initial_offsets(self) -> None:
        """Set byte offsets to end of file so we only get new events from the watcher.

        The initial load is handled separately by get_all_events().
        """
        for state in self._session_states.values():
            if state.file_path.exists():
                try:
                    stat = state.file_path.stat()
                    state.byte_offset = stat.st_size
                    state.last_mtime = stat.st_mtime
                    state.last_size = stat.st_size
                except OSError:
                    pass

    def _poll_for_changes(self) -> None:
        """Check all session files for new content."""
        for state in self._session_states.values():
            if not state.file_path.exists():
                continue

            try:
                stat = state.file_path.stat()
            except OSError:
                continue

            # mtime/size check -- skip if unchanged
            current_mtime = stat.st_mtime
            current_size = stat.st_size
            if current_mtime == state.last_mtime and current_size == state.last_size:
                continue

            state.last_mtime = current_mtime
            state.last_size = current_size

            if current_size <= state.byte_offset:
                continue

            # Read new bytes
            try:
                with open(state.file_path, "rb") as f:
                    f.seek(state.byte_offset)
                    new_data = f.read()
                state.byte_offset = state.byte_offset + len(new_data)
            except OSError:
                continue

            new_lines = new_data.decode("utf-8", errors="replace").splitlines()
            if not new_lines:
                continue

            new_events = parse_session_lines(
                new_lines,
                existing_event_ids=self._existing_event_ids,
                tool_name_by_call_id=self._tool_name_by_call_id,
            )

            if new_events:
                self._on_events(self._agent_id, new_events)
