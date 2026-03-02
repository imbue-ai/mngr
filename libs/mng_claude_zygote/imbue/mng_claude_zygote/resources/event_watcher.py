#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["watchdog"]
# ///
"""Event watcher for changeling agents.

Watches event log files for new entries and sends unhandled events to
the primary agent. Uses watchdog for fast filesystem event detection,
with periodic mtime-based polling as a safety net.

Usage: uv run event_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
  MNG_AGENT_NAME       - name of the primary agent to send messages to
  MNG_HOST_DIR         - host data directory (contains logs/ for log output)
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from watchdog.events import FileSystemEvent
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


@dataclasses.dataclass(frozen=True)
class _WatcherSettings:
    """Parsed watcher settings from settings.toml."""

    poll_interval: int = 3
    sources: list[str] = dataclasses.field(default_factory=lambda: ["messages", "scheduled", "mng_agents", "stop"])


class _Logger:
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


def _load_watcher_settings(agent_state_dir: Path) -> _WatcherSettings:
    """Load watcher settings from settings.toml, falling back to defaults."""
    settings_path = agent_state_dir / "settings.toml"
    try:
        if not settings_path.exists():
            return _WatcherSettings()
        raw = tomllib.loads(settings_path.read_text())
        watchers = raw.get("watchers", {})
        return _WatcherSettings(
            poll_interval=watchers.get("event_poll_interval_seconds", 3),
            sources=watchers.get("watched_event_sources", _WatcherSettings().sources),
        )
    except Exception as exc:
        print(f"WARNING: failed to load settings: {exc}", file=sys.stderr)
        return _WatcherSettings()


def _get_offset(offsets_dir: Path, source: str) -> int:
    """Read the current line offset for a source."""
    offset_file = offsets_dir / f"{source}.offset"
    try:
        return int(offset_file.read_text().strip())
    except (OSError, ValueError):
        return 0


def _set_offset(offsets_dir: Path, source: str, offset: int) -> None:
    """Write the current line offset for a source."""
    offset_file = offsets_dir / f"{source}.offset"
    offset_file.write_text(str(offset))


def _check_and_send_new_events(
    events_file: Path,
    source: str,
    offsets_dir: Path,
    agent_name: str,
    log: _Logger,
) -> None:
    """Check for new lines in an events.jsonl file and send them via mng message."""
    if not events_file.is_file():
        return

    current_offset = _get_offset(offsets_dir, source)

    try:
        with events_file.open() as f:
            all_lines = f.readlines()
    except OSError as exc:
        log.info(f"ERROR: failed to read {events_file}: {exc}")
        return

    total_lines = len(all_lines)
    if total_lines <= current_offset:
        return

    new_lines = all_lines[current_offset:total_lines]
    new_text = "".join(new_lines).strip()
    if not new_text:
        return

    new_count = total_lines - current_offset
    log.info(f"Found {new_count} new event(s) from source '{source}' (offset {current_offset} -> {total_lines})")
    log.debug(f"New events from {source}: {new_text[:500]}")

    message = f"New {source} event(s):\n{new_text}"

    log.info(f"Sending {new_count} event(s) from '{source}' to agent '{agent_name}'")
    try:
        result = subprocess.run(
            ["uv", "run", "mng", "message", agent_name, "-m", message],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        log.info(f"ERROR: timed out sending events from {source} to {agent_name}")
        return
    except OSError as exc:
        log.info(f"ERROR: failed to invoke mng message subprocess: {exc}")
        return

    if result.returncode != 0:
        log.info(f"ERROR: mng message returned non-zero for {source} -> {agent_name}: {result.stderr}")
        return

    try:
        _set_offset(offsets_dir, source, total_lines)
    except OSError as exc:
        log.info(f"ERROR: failed to write offset file for {source}: {exc}")
        return

    log.info(f"Events sent successfully, offset updated to {total_lines}")


def _check_all_sources(
    logs_dir: Path,
    watched_sources: list[str],
    offsets_dir: Path,
    agent_name: str,
    log: _Logger,
) -> None:
    """Check all watched sources for new events."""
    for source in watched_sources:
        events_file = logs_dir / source / "events.jsonl"
        _check_and_send_new_events(events_file, source, offsets_dir, agent_name, log)


def _mtime_poll(
    logs_dir: Path,
    watched_sources: list[str],
    mtime_cache: dict[str, tuple[float, int]],
    log: _Logger,
) -> bool:
    """Scan all watched event files for mtime/size changes.

    Returns True if any file was created, removed, or modified since the
    last scan. This catches changes that watchdog may have missed.
    """
    is_changed = False
    current_keys: set[str] = set()

    for source in watched_sources:
        source_dir = logs_dir / source
        if not source_dir.exists():
            continue
        try:
            for entry in source_dir.iterdir():
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
            log.debug(f"Failed to list directory {source_dir}: {exc}")
            continue

    # Detect removed files
    removed_keys = set(mtime_cache.keys()) - current_keys
    for key in removed_keys:
        del mtime_cache[key]
        is_changed = True
        log.debug(f"File removed: {key}")

    return is_changed


def _require_env(name: str) -> str:
    """Read a required environment variable, exiting if unset."""
    value = os.environ.get(name, "")
    if not value:
        print(f"ERROR: {name} must be set", file=sys.stderr)
        sys.exit(1)
    return value


# --- WATCHDOG-DEPENDENT CODE BELOW (not importable without watchdog) ---


class _ChangeHandler(FileSystemEventHandler):
    """Watchdog handler that signals the main loop on any filesystem change."""

    def __init__(self, wake_event: threading.Event) -> None:
        super().__init__()
        self._wake_event = wake_event

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._wake_event.set()


def _setup_watchdog(
    watch_dirs: list[Path],
    wake_event: threading.Event,
    log: _Logger,
) -> tuple[Observer, bool]:
    """Create and start a watchdog Observer for the given directories.

    Returns (observer, is_active). If the observer fails to start,
    is_active is False and the caller should fall back to polling only.
    """
    handler = _ChangeHandler(wake_event)
    observer = Observer()
    try:
        for source_dir in watch_dirs:
            observer.schedule(handler, str(source_dir), recursive=False)
        observer.start()
        return observer, True
    except Exception as exc:
        log.info(f"WARNING: watchdog observer failed to start, falling back to polling only: {exc}")
        return observer, False


def _run_event_loop(
    logs_dir: Path,
    watched_sources: list[str],
    offsets_dir: Path,
    agent_name: str,
    poll_interval: int,
    wake_event: threading.Event,
    log: _Logger,
) -> None:
    """Run the main event loop: wait for watchdog or poll timeout, then check sources."""
    mtime_cache: dict[str, tuple[float, int]] = {}
    _mtime_poll(logs_dir, watched_sources, mtime_cache, log)

    while True:
        is_triggered_by_watchdog = wake_event.wait(timeout=poll_interval)
        wake_event.clear()

        if is_triggered_by_watchdog:
            log.debug("Woken by watchdog filesystem event")

        # Always update the mtime cache so it stays in sync. On timeout,
        # this also serves as the safety-net poll for missed watchdog events.
        is_mtime_changed = _mtime_poll(logs_dir, watched_sources, mtime_cache, log)
        if not is_triggered_by_watchdog and is_mtime_changed:
            log.info("Periodic mtime poll detected changes")

        _check_all_sources(logs_dir, watched_sources, offsets_dir, agent_name, log)


def main() -> None:
    agent_state_dir = Path(_require_env("MNG_AGENT_STATE_DIR"))
    agent_name = _require_env("MNG_AGENT_NAME")
    host_dir = Path(_require_env("MNG_HOST_DIR"))

    logs_dir = agent_state_dir / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True, exist_ok=True)

    log = _Logger(host_dir / "logs" / "event_watcher.log")

    settings = _load_watcher_settings(agent_state_dir)

    log.info("Event watcher started")
    log.info(f"  Agent data dir: {agent_state_dir}")
    log.info(f"  Agent name: {agent_name}")
    log.info(f"  Watched sources: {' '.join(settings.sources)}")
    log.info(f"  Offsets dir: {offsets_dir}")
    log.info(f"  Log file: {log.log_file_path}")
    log.info(f"  Poll interval: {settings.poll_interval}s")
    log.info("  Using watchdog for file watching with periodic mtime polling")

    # Ensure watched directories exist (watchdog needs them to exist)
    watch_dirs: list[Path] = []
    for source in settings.sources:
        source_dir = logs_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        watch_dirs.append(source_dir)

    wake_event = threading.Event()
    observer, is_watchdog_active = _setup_watchdog(watch_dirs, wake_event, log)

    try:
        _run_event_loop(logs_dir, settings.sources, offsets_dir, agent_name, settings.poll_interval, wake_event, log)
    except KeyboardInterrupt:
        log.info("Event watcher stopping (KeyboardInterrupt)")
    finally:
        if is_watchdog_active:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
