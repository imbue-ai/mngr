"""Watch the per-agent ``active`` and ``permissions_waiting`` marker files.

The Claude readiness hooks (``mngr_claude.claude_config.build_readiness_hooks_config``)
touch and remove these files inside ``$MNGR_AGENT_STATE_DIR/`` to signal where
the agent is in its turn lifecycle. We use ``watchdog`` for sub-second
reaction, mirroring the pattern from ``agent_manager._ApplicationsFileHandler``.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

ACTIVE_MARKER_FILENAME = "active"
PERMISSIONS_WAITING_MARKER_FILENAME = "permissions_waiting"

_WATCHED_BASENAMES = frozenset({ACTIVE_MARKER_FILENAME, PERMISSIONS_WAITING_MARKER_FILENAME})


class _MarkerFileHandler(FileSystemEventHandler):
    """Fires the on_change callback whenever an ``active`` or ``permissions_waiting`` event arrives.

    Filters by basename so unrelated files in the agent state directory (e.g.
    ``claude_session_id``, ``session_started``) don't trigger broadcasts.
    """

    on_change: Callable[[], None]

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths: list[str | bytes] = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        for raw_path in paths:
            if not raw_path:
                continue
            decoded = raw_path.decode("utf-8", errors="replace") if isinstance(raw_path, bytes) else raw_path
            if Path(decoded).name in _WATCHED_BASENAMES:
                self.on_change()
                return


def _make_marker_file_handler(on_change: Callable[[], None]) -> _MarkerFileHandler:
    """Create a marker-file handler bound to ``on_change``."""
    handler = _MarkerFileHandler()
    handler.on_change = on_change
    return handler


class AgentMarkerWatcher:
    """Watches the marker-file pair for a single agent.

    The agent state directory is created on start so the watchdog has a real
    directory to attach to even before the agent's hooks have fired for the
    first time.
    """

    _agent_id: str
    _agent_state_dir: Path
    _on_change: Callable[[str], None]
    _observer: Any

    @classmethod
    def build(
        cls,
        agent_id: str,
        agent_state_dir: Path,
        on_change: Callable[[str], None],
    ) -> "AgentMarkerWatcher":
        """Build a watcher bound to a single agent's state directory."""
        instance = cls.__new__(cls)
        instance._agent_id = agent_id
        instance._agent_state_dir = agent_state_dir
        instance._on_change = on_change
        instance._observer = None
        return instance

    def start(self) -> None:
        try:
            self._agent_state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _loguru_logger.exception(
                "Failed to ensure marker directory for agent {} at {}",
                self._agent_id,
                self._agent_state_dir,
            )
            return

        observer = _Observer()
        handler = _make_marker_file_handler(lambda: self._on_change(self._agent_id))
        try:
            observer.schedule(handler, str(self._agent_state_dir), recursive=False)
            observer.daemon = True
            observer.start()
        except OSError:
            _loguru_logger.exception(
                "Failed to start marker watcher for agent {} at {}",
                self._agent_id,
                self._agent_state_dir,
            )
            return
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    def read_markers(self) -> tuple[bool, bool]:
        """Return ``(active_present, permissions_waiting_present)``."""
        active = (self._agent_state_dir / ACTIVE_MARKER_FILENAME).exists()
        permissions_waiting = (self._agent_state_dir / PERMISSIONS_WAITING_MARKER_FILENAME).exists()
        return active, permissions_waiting
