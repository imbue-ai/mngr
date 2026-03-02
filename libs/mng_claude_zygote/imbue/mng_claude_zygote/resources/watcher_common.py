"""Shared utilities for changeling watcher scripts.

This module is provisioned alongside the watcher scripts (event_watcher.py,
conversation_watcher.py) to $MNG_HOST_DIR/commands/ and imported by them at
runtime. It provides the common watchdog integration, logging, and polling
infrastructure that all watchers share.

Watchdog imports and classes that depend on them are placed below the
``# --- WATCHDOG-DEPENDENT CODE BELOW`` marker. Test helpers strip
everything below this marker so they can exec the stdlib-only portion
without the watchdog dependency installed.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path


class Logger:
    """Simple dual-output logger: writes to both stdout and a log file."""

    def __init__(self, log_file: Path) -> None:
        self.log_file_path = log_file
        self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

    def _timestamp(self) -> str:
        now = time.time()
        fractional_ns = int((now % 1) * 1_000_000_000)
        utc_struct = time.gmtime(now)
        return time.strftime("%Y-%m-%dT%H:%M:%S", utc_struct) + f".{fractional_ns:09d}Z"

    def info(self, msg: str) -> None:
        line = f"[{self._timestamp()}] {msg}"
        print(line, flush=True)
        try:
            with self.log_file_path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def debug(self, msg: str) -> None:
        line = f"[{self._timestamp()}] [debug] {msg}"
        try:
            with self.log_file_path.open("a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def require_env(name: str) -> str:
    """Read a required environment variable, exiting if unset."""
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: {name} must be set", file=sys.stderr)
        sys.exit(1)
    return value


def mtime_poll_files(
    watch_paths: list[Path],
    mtime_cache: dict[str, tuple[float, int]],
    log: Logger,
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
                log.debug(f"New file detected: {file_path}")
            else:
                log.debug(f"File changed: {file_path}")

    removed_keys = set(mtime_cache.keys()) - current_keys
    for key in removed_keys:
        del mtime_cache[key]
        is_changed = True
        log.debug(f"File removed: {key}")

    return is_changed


def mtime_poll_directories(
    directories: list[Path],
    mtime_cache: dict[str, tuple[float, int]],
    log: Logger,
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
                    # File may have been deleted between iterdir() and stat()
                    continue

                previous = mtime_cache.get(key)
                if previous != current:
                    mtime_cache[key] = current
                    is_changed = True
                    if previous is None:
                        log.debug(f"New file detected: {entry}")
                    else:
                        log.debug(f"File changed: {entry}")
        except OSError as exc:
            log.debug(f"Failed to list directory {directory}: {exc}")
            continue

    removed_keys = set(mtime_cache.keys()) - current_keys
    for key in removed_keys:
        del mtime_cache[key]
        is_changed = True
        log.debug(f"File removed: {key}")

    return is_changed


# --- WATCHDOG-DEPENDENT CODE BELOW (not importable without watchdog) ---

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that signals the main loop on any filesystem change."""

    def __init__(self, wake_event: threading.Event) -> None:
        super().__init__()
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._wake_event.set()


def setup_watchdog_for_directories(
    watch_dirs: list[Path],
    wake_event: threading.Event,
    log: Logger,
) -> tuple[Observer, bool]:
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
        log.info(f"WARNING: watchdog observer failed to start, falling back to polling only: {exc}")
        return observer, False


def setup_watchdog_for_files(
    watch_paths: list[Path],
    wake_event: threading.Event,
    log: Logger,
) -> tuple[Observer, bool]:
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
                log.info(f"WARNING: failed to watch {parent}: {exc}")

    try:
        observer.start()
        return observer, True
    except Exception as exc:
        log.info(f"WARNING: watchdog observer failed to start, falling back to polling only: {exc}")
        return observer, False
