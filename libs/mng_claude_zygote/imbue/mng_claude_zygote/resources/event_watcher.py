#!/usr/bin/env python3
"""Event watcher for changeling agents.

Watches event log files for new entries and sends unhandled events to
the primary agent. Uses watchdog for fast filesystem event detection,
with periodic mtime-based polling as a safety net.

Usage: python3 event_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
  MNG_AGENT_NAME       - name of the primary agent to send messages to
  MNG_HOST_DIR         - host data directory (contains logs/ for log output)
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

from loguru import logger

try:
    from imbue.mng_claude_zygote.resources.watcher_common import load_watchers_section
    from imbue.mng_claude_zygote.resources.watcher_common import require_env
    from imbue.mng_claude_zygote.resources.watcher_common import run_watcher_loop
    from imbue.mng_claude_zygote.resources.watcher_common import setup_watcher_logging
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from watcher_common import load_watchers_section  # type: ignore[no-redef]
    from watcher_common import require_env  # type: ignore[no-redef]
    from watcher_common import run_watcher_loop  # type: ignore[no-redef]
    from watcher_common import setup_watcher_logging  # type: ignore[no-redef]


_DEFAULT_SOURCES = ["messages", "scheduled", "mng_agents", "stop"]


@dataclasses.dataclass(frozen=True)
class _WatcherSettings:
    """Parsed watcher settings from settings.toml."""

    poll_interval: int = 3
    sources: list[str] = dataclasses.field(default_factory=lambda: list(_DEFAULT_SOURCES))


def _load_watcher_settings(agent_work_dir: Path) -> _WatcherSettings:
    """Load watcher settings from settings.toml, falling back to defaults."""
    watchers = load_watchers_section(agent_work_dir)
    if not watchers:
        return _WatcherSettings()
    return _WatcherSettings(
        poll_interval=watchers.get("event_poll_interval_seconds", 3),
        sources=watchers.get("watched_event_sources", list(_DEFAULT_SOURCES)),
    )


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
) -> None:
    """Check for new lines in an events.jsonl file and send them via mng message."""
    if not events_file.is_file():
        return

    current_offset = _get_offset(offsets_dir, source)

    try:
        with events_file.open() as f:
            all_lines = f.readlines()
    except OSError as exc:
        logger.error("Failed to read {}: {}", events_file, exc)
        return

    total_lines = len(all_lines)
    if total_lines <= current_offset:
        return

    new_lines = all_lines[current_offset:total_lines]
    new_text = "".join(new_lines).strip()
    if not new_text:
        return

    new_count = total_lines - current_offset
    logger.info(
        "Found {} new event(s) from source '{}' (offset {} -> {})", new_count, source, current_offset, total_lines
    )
    logger.debug("New events from {}: {}", source, new_text[:500])

    message = f"New {source} event(s):\n{new_text}"

    logger.info("Sending {} event(s) from '{}' to agent '{}'", new_count, source, agent_name)
    try:
        result = subprocess.run(
            ["uv", "run", "mng", "message", agent_name, "-m", message],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("Timed out sending events from {} to {}", source, agent_name)
        return
    except OSError as exc:
        logger.error("Failed to invoke mng message subprocess: {}", exc)
        return

    if result.returncode != 0:
        logger.error("mng message returned non-zero for {} -> {}: {}", source, agent_name, result.stderr)
        return

    try:
        _set_offset(offsets_dir, source, total_lines)
    except OSError as exc:
        logger.error("Failed to write offset file for {}: {}", source, exc)
        return

    logger.info("Events sent successfully, offset updated to {}", total_lines)


def _check_all_sources(
    logs_dir: Path,
    watched_sources: list[str],
    offsets_dir: Path,
    agent_name: str,
) -> None:
    """Check all watched sources for new events."""
    for source in watched_sources:
        events_file = logs_dir / source / "events.jsonl"
        _check_and_send_new_events(events_file, source, offsets_dir, agent_name)


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_work_dir = Path(require_env("MNG_AGENT_WORK_DIR"))
    agent_name = require_env("MNG_AGENT_NAME")
    host_dir = Path(require_env("MNG_HOST_DIR"))

    logs_dir = agent_state_dir / "logs"
    offsets_dir = logs_dir / ".event_offsets"
    offsets_dir.mkdir(parents=True, exist_ok=True)

    setup_watcher_logging("event_watcher", host_dir / "logs")

    settings = _load_watcher_settings(agent_work_dir)

    logger.info("Event watcher started")
    logger.info("  Agent data dir: {}", agent_state_dir)
    logger.info("  Agent name: {}", agent_name)
    logger.info("  Watched sources: {}", " ".join(settings.sources))
    logger.info("  Offsets dir: {}", offsets_dir)
    logger.info("  Poll interval: {}s", settings.poll_interval)

    # Ensure watched directories exist (watchdog needs them to exist)
    watch_dirs: list[Path] = []
    for source in settings.sources:
        source_dir = logs_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        watch_dirs.append(source_dir)

    def on_tick() -> None:
        _check_all_sources(logs_dir, settings.sources, offsets_dir, agent_name)

    run_watcher_loop(
        "Event watcher",
        settings.poll_interval,
        watch_dirs,
        is_directory_mode=True,
        on_tick=on_tick,
    )


if __name__ == "__main__":
    main()
