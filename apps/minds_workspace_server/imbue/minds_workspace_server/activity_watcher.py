"""Watch the per-agent ``permissions_waiting`` marker file.

The Claude readiness hooks (``mngr_claude.claude_config.build_readiness_hooks_config``)
touch and remove this file inside ``$MNGR_AGENT_STATE_DIR/`` to signal that Claude
is blocked on a permission prompt. We use ``watchdog`` for sub-second reaction,
mirroring the pattern from ``agent_manager._ApplicationsFileHandler``.

The legacy ``active`` marker is intentionally not watched here: it can be left
behind on abnormal Claude exit, leading to a stale "Thinking..." indicator. The
session transcript is the authoritative source for the IDLE / THINKING /
TOOL_RUNNING states.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger
from watchdog.events import FileMovedEvent
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer as _Observer

PERMISSIONS_WAITING_MARKER_FILENAME = "permissions_waiting"


class _MarkerFileHandler(FileSystemEventHandler):
    """Fires the on_change callback whenever a ``permissions_waiting`` event arrives.

    Filters by basename so unrelated files in the agent state directory (e.g.
    ``claude_session_id``, ``session_started``) don't trigger broadcasts.
    """

    on_change: Callable[[], None]

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        paths = [event.src_path]
        if isinstance(event, FileMovedEvent):
            paths.append(event.dest_path)
        if any(os.path.basename(p) == PERMISSIONS_WAITING_MARKER_FILENAME for p in paths):
            self.on_change()


def _make_marker_file_handler(on_change: Callable[[], None]) -> _MarkerFileHandler:
    """Create a marker-file handler bound to ``on_change``."""
    handler = _MarkerFileHandler()
    handler.on_change = on_change
    return handler


class AgentMarkerWatcher:
    """Watches the ``permissions_waiting`` marker file for a single agent.

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
        except OSError as exc:
            _loguru_logger.opt(exception=exc).error(
                "Failed to ensure marker directory for agent {} at {}",
                self._agent_id,
                self._agent_state_dir,
            )
            return

        observer = _Observer()
        handler = _make_marker_file_handler(lambda: self._on_change(self._agent_id))
        observer.schedule(handler, str(self._agent_state_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def request_stop(self) -> None:
        """Signal the watchdog observer to stop without waiting for it to join.

        Pair with :meth:`wait_stopped` for fan-out shutdown across many
        watchers; use :meth:`stop` for the per-agent serial shutdown path.
        """
        if self._observer is not None:
            self._observer.stop()

    def wait_stopped(self, timeout: float = 5.0) -> None:
        """Join the watchdog observer thread that ``request_stop`` signalled.

        Intended to be called after :meth:`request_stop`. Joins the observer
        thread with a timeout and then clears the cached reference. If called
        without a prior ``request_stop``, the join will block until the
        timeout elapses, since the observer thread is still running. A second
        call after the reference has been cleared is a no-op.
        """
        if self._observer is not None:
            self._observer.join(timeout=timeout)
            self._observer = None

    def stop(self) -> None:
        """Stop and join the watchdog observer in a single synchronous call."""
        self.request_stop()
        self.wait_stopped()

    def read_permissions_waiting(self) -> bool:
        """Return True iff the ``permissions_waiting`` marker file exists."""
        return (self._agent_state_dir / PERMISSIONS_WAITING_MARKER_FILENAME).exists()
