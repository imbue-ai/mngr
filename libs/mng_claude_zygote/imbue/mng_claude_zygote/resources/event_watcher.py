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
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

# watcher_common.py is provisioned alongside this script to the same directory
sys.path.insert(0, str(Path(__file__).parent))
from watcher_common import Logger
from watcher_common import mtime_poll_directories
from watcher_common import require_env
from watcher_common import setup_watchdog_for_directories


@dataclasses.dataclass(frozen=True)
class _WatcherSettings:
    """Parsed watcher settings from settings.toml."""

    poll_interval: int = 3
    sources: list[str] = dataclasses.field(default_factory=lambda: ["messages", "scheduled", "mng_agents", "stop"])


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
    log: Logger,
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
    log: Logger,
) -> None:
    """Check all watched sources for new events."""
    for source in watched_sources:
        events_file = logs_dir / source / "events.jsonl"
        _check_and_send_new_events(events_file, source, offsets_dir, agent_name, log)


# --- WATCHDOG-DEPENDENT CODE BELOW (not importable without watchdog) ---


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_name = require_env("MNG_AGENT_NAME")
    host_dir = Path(require_env("MNG_HOST_DIR"))

    logs_dir = agent_state_dir / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True, exist_ok=True)

    log = Logger(host_dir / "logs" / "event_watcher.log")

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
    observer, is_watchdog_active = setup_watchdog_for_directories(watch_dirs, wake_event, log)

    # Initialize mtime cache
    mtime_cache: dict[str, tuple[float, int]] = {}
    mtime_poll_directories(watch_dirs, mtime_cache, log)

    try:
        while True:
            is_triggered_by_watchdog = wake_event.wait(timeout=settings.poll_interval)
            wake_event.clear()

            if is_triggered_by_watchdog:
                log.debug("Woken by watchdog filesystem event")

            # Always update mtime cache; on timeout this catches missed watchdog events
            is_mtime_changed = mtime_poll_directories(watch_dirs, mtime_cache, log)
            if not is_triggered_by_watchdog and is_mtime_changed:
                log.info("Periodic mtime poll detected changes")

            _check_all_sources(logs_dir, settings.sources, offsets_dir, agent_name, log)
    except KeyboardInterrupt:
        log.info("Event watcher stopping (KeyboardInterrupt)")
    finally:
        if is_watchdog_active:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
