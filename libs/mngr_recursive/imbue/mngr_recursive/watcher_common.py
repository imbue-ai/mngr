"""Shared utilities for agent supporting service scripts.

Provides common watchdog integration, logging, and polling infrastructure
used by supporting services in mngr_llm (conversation_watcher, webchat_server)
and mngr_claude_mind (event_watcher).

Lives in mngr_recursive so that all plugins that need watcher infrastructure
can depend on it without introducing circular dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import tomllib
from collections.abc import Callable
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class MngrNotInstalledError(RuntimeError):
    """Raised when the per-agent mngr binary cannot be found."""


def get_mngr_command() -> list[str]:
    """Return the command for invoking the per-agent mngr binary.

    Looks for the mngr binary in ``$UV_TOOL_BIN_DIR/mngr``. This env var
    is set by ``ClaudeMindAgent.modify_env_vars()`` during agent
    creation and points to the per-agent bin directory where
    ``uv tool install`` places the mngr entrypoint.

    Raises MngrNotInstalledError if the binary cannot be found, which
    indicates that mngr was not properly provisioned for this agent.
    """
    bin_dir = os.environ.get("UV_TOOL_BIN_DIR", "")
    if not bin_dir:
        raise MngrNotInstalledError(
            "UV_TOOL_BIN_DIR is not set. The per-agent mngr binary cannot be located without it."
        )
    mngr_bin = os.path.join(bin_dir, "mngr")
    if not os.path.isfile(mngr_bin):
        raise MngrNotInstalledError(
            f"Per-agent mngr binary not found at {mngr_bin}. "
            "Ensure the mngr_recursive plugin is enabled and provisioning completed successfully."
        )
    return [mngr_bin]


DEFAULT_CEL_INCLUDE_FILTERS: Final[tuple[str, ...]] = (
    # only include log events if they are ERROR or WARNING level
    '!source.startsWith("logs/") || (source.startsWith("logs/") && (level == "ERROR" || level == "WARNING"))',
)

DEFAULT_CEL_EXCLUDE_FILTERS: Final[tuple[str, ...]] = (
    # delivery_failures is about the delivery of messages to the core thinking loop, so it would never see these anyway
    'source == "delivery_failures"',
    # handled_events is just startup noise from mngr
    'source == "handled_events"',
    # raw agent state messages
    'source == "mngr/agents"',
    # mngr/agent_states events for non-mind agents
    """source == 'mngr/agent_states' && !(has(agent.labels.mind))""",
    # server_registered events are infrastructure used by the forwarding server for backend discovery
    'source == "servers"',
)


def setup_watcher_logging(watcher_name: str, log_dir: Path) -> None:
    """Configure loguru for a watcher process.

    Sets up:
    - stdout logging for DEBUG+ messages (timestamped)
    - JSONL file logging for DEBUG+ to <log_dir>/<watcher_name>/events.jsonl
    """
    logger.remove()

    logger.add(
        sys.stdout,
        level="DEBUG",
        format="[{time:YYYY-MM-DD HH:mm:ss.SSSSSS!UTC}] {message}",
        colorize=False,
    )

    log_file = log_dir / watcher_name / "events.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    sink = _make_jsonl_file_sink(
        file_path=str(log_file),
        event_type="watcher",
        event_source=f"logs/{watcher_name}",
    )
    logger.add(
        sink,
        level="DEBUG",
        format="{message}",
        colorize=False,
    )


def _format_nanosecond_timestamp(dt: Any) -> str:
    """Format a datetime as ISO 8601 with nanosecond precision in UTC."""
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_dt.microsecond * 1000:09d}Z"


def _make_jsonl_file_sink(
    file_path: str,
    event_type: str,
    event_source: str,
    max_size_bytes: int = 10 * 1024 * 1024,
) -> Callable[..., None]:
    """Create a loguru sink function that writes flat JSONL to a rotating file."""
    state: dict[str, Any] = {"file": None, "size": 0}

    def _ensure_file() -> Any:
        if state["file"] is None:
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            state["file"] = open(file_path, "a")
            try:
                state["size"] = Path(file_path).stat().st_size
            except OSError:
                state["size"] = 0
        return state["file"]

    def _rotate_if_needed() -> None:
        if state["size"] >= max_size_bytes:
            if state["file"] is not None:
                state["file"].close()
                state["file"] = None
            path = Path(file_path)
            rotation_idx = next(idx for idx in range(1, 10000) if not path.with_name(f"{path.name}.{idx}").exists())
            path.rename(path.with_name(f"{path.name}.{rotation_idx}"))
            state["size"] = 0

    def sink(message: Any) -> None:
        record = message.record
        event: dict[str, Any] = {
            "timestamp": _format_nanosecond_timestamp(record["time"]),
            "type": event_type,
            "event_id": f"evt-{uuid4().hex}",
            "source": event_source,
            "level": record["level"].name,
            "message": record["message"],
            "pid": os.getpid(),
        }

        json_line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        line_bytes = len(json_line.encode("utf-8"))

        _rotate_if_needed()
        fh = _ensure_file()
        fh.write(json_line)
        fh.flush()
        state["size"] += line_bytes

    return sink


def require_env(name: str) -> str:
    """Read a required environment variable, exiting if unset."""
    value = os.environ.get(name, "")
    if not value:
        logger.error("{} must be set", name)
        sys.exit(1)
    return value


def read_event_ids_from_jsonl(file_path: Path) -> set[str]:
    """Read event_id values from a JSONL file into a set.

    Skips lines that are empty, malformed JSON, or missing the event_id key.
    Returns an empty set if the file does not exist.
    """
    event_ids: set[str] = set()
    if not file_path.is_file():
        return event_ids
    try:
        with file_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event_ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Malformed event line in {}: {}", file_path, exc)
                    continue
    except OSError as exc:
        logger.warning("Failed to read {}: {}", file_path, exc)
    return event_ids


def load_watchers_section(agent_work_dir: Path) -> dict[str, Any]:
    """Load the [watchers] section from minds.toml.

    Returns an empty dict on any error (missing file, corrupt TOML, etc.).
    """
    settings_path = agent_work_dir / "minds.toml"
    try:
        if not settings_path.exists():
            return {}
        raw = tomllib.loads(settings_path.read_text())
        return raw.get("watchers", {})
    except (OSError, tomllib.TOMLDecodeError, ValueError, KeyError) as exc:
        logger.warning("Failed to load watcher settings: {}", exc)
        return {}


def mtime_poll_files(
    watch_paths: list[Path],
    mtime_cache: dict[str, tuple[float, int]],
) -> bool:
    """Check specific files for mtime/size changes. Returns True if any changed."""
    is_changed = False
    current_keys: set[str] = set()

    for file_path in watch_paths:
        key = str(file_path)
        current_keys.add(key)
        try:
            stat = file_path.stat()
            current = (stat.st_mtime, stat.st_size)
        except OSError:
            if key in mtime_cache:
                del mtime_cache[key]
                is_changed = True
            continue

        previous = mtime_cache.get(key)
        if previous != current:
            mtime_cache[key] = current
            is_changed = True
            if previous is None:
                logger.debug("New file detected: {}", file_path)
            else:
                logger.debug("File changed: {}", file_path)

    removed_keys = set(mtime_cache.keys()) - current_keys
    for key in removed_keys:
        del mtime_cache[key]
        is_changed = True
        logger.debug("File removed: {}", key)

    return is_changed


def mtime_poll_directories(
    directories: list[Path],
    mtime_cache: dict[str, tuple[float, int]],
) -> bool:
    """Scan directories for mtime/size changes in their contents.

    Returns True if any file was created, removed, or modified since the
    last scan. This catches changes that watchdog may have missed.
    """
    is_changed = False
    current_keys: set[str] = set()

    for directory in directories:
        if not directory.exists():
            continue
        try:
            for entry in directory.iterdir():
                key = str(entry)
                current_keys.add(key)
                try:
                    stat = entry.stat()
                    current = (stat.st_mtime, stat.st_size)
                except OSError:
                    continue

                previous = mtime_cache.get(key)
                if previous != current:
                    mtime_cache[key] = current
                    is_changed = True
                    if previous is None:
                        logger.debug("New file detected: {}", entry)
                    else:
                        logger.debug("File changed: {}", entry)
        except OSError as exc:
            logger.debug("Failed to list directory {}: {}", directory, exc)
            continue

    removed_keys = set(mtime_cache.keys()) - current_keys
    for key in removed_keys:
        del mtime_cache[key]
        is_changed = True
        logger.debug("File removed: {}", key)

    return is_changed


_NON_CHANGE_EVENT_TYPES: Final[frozenset[str]] = frozenset({"opened", "closed", "closed_no_write"})


class ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that signals the main loop on actual filesystem changes.

    Ignores events that do not represent modifications (file opened, file
    closed, file closed without write) since these are read-only operations
    that should not trigger processing.
    """

    def __init__(self, wake_event: threading.Event) -> None:
        super().__init__()
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _NON_CHANGE_EVENT_TYPES:
            return
        self._wake_event.set()


def setup_watchdog_for_directories(
    watch_dirs: list[Path],
    wake_event: threading.Event,
) -> tuple[Any, bool]:
    """Create and start a watchdog Observer for the given directories.

    Returns (observer, is_active). If the observer fails to start,
    is_active is False and the caller should fall back to polling only.
    """
    handler = ChangeHandler(wake_event)
    observer = Observer()
    try:
        for source_dir in watch_dirs:
            observer.schedule(handler, str(source_dir), recursive=False)
        observer.start()
        return observer, True
    except Exception as exc:
        logger.warning("Watchdog observer failed to start, falling back to polling only: {}", exc)
        return observer, False


def setup_watchdog_for_files(
    watch_paths: list[Path],
    wake_event: threading.Event,
) -> tuple[Any, bool]:
    """Create and start a watchdog Observer for the parent directories of watched files.

    Returns (observer, is_active). If the observer fails to start,
    is_active is False and the caller should fall back to polling only.
    """
    handler = ChangeHandler(wake_event)
    observer = Observer()

    watched_dirs: set[str] = set()
    for file_path in watch_paths:
        parent = str(file_path.parent)
        if parent not in watched_dirs:
            try:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                observer.schedule(handler, parent, recursive=False)
                watched_dirs.add(parent)
            except Exception as exc:
                logger.warning("Failed to watch {}: {}", parent, exc)

    try:
        observer.start()
        return observer, True
    except Exception as exc:
        logger.warning("Watchdog observer failed to start, falling back to polling only: {}", exc)
        return observer, False


def run_watcher_loop(
    watcher_name: str,
    poll_interval: int,
    watch_targets: list[Path],
    *,
    is_directory_mode: bool,
    on_tick: Callable[[], None],
) -> None:
    """Run the common watcher main loop with watchdog + mtime polling.

    Sets up watchdog for filesystem event detection, initializes mtime polling
    as a safety net, then loops: wait for events or timeout, poll for changes,
    and invoke the on_tick callback.
    """
    wake_event = threading.Event()

    if is_directory_mode:
        observer, is_watchdog_active = setup_watchdog_for_directories(watch_targets, wake_event)
    else:
        observer, is_watchdog_active = setup_watchdog_for_files(watch_targets, wake_event)

    mtime_cache: dict[str, tuple[float, int]] = {}
    if is_directory_mode:
        mtime_poll_directories(watch_targets, mtime_cache)
    else:
        mtime_poll_files(watch_targets, mtime_cache)

    try:
        while True:
            is_triggered_by_watchdog = wake_event.wait(timeout=poll_interval)
            wake_event.clear()

            if is_directory_mode:
                is_mtime_changed = mtime_poll_directories(watch_targets, mtime_cache)
            else:
                is_mtime_changed = mtime_poll_files(watch_targets, mtime_cache)

            if not is_triggered_by_watchdog and is_mtime_changed:
                logger.info("Periodic mtime poll detected changes")

            on_tick()
    except KeyboardInterrupt:
        logger.info("{} stopping (KeyboardInterrupt)", watcher_name)
    finally:
        if is_watchdog_active:
            observer.stop()
            observer.join()
