#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["watchdog"]
# ///
"""Conversation watcher for changeling agents.

Syncs messages from the llm database to the standard event log at
logs/messages/events.jsonl. Uses watchdog for fast filesystem event
detection, with periodic mtime-based polling as a safety net.

Usage: uv run conversation_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains logs/)
  MNG_HOST_DIR         - host data directory (contains logs/ for log output)
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import sys
import threading
import tomllib
from pathlib import Path

# watcher_common.py is provisioned alongside this script to the same directory
sys.path.insert(0, str(Path(__file__).parent))
from watcher_common import Logger
from watcher_common import mtime_poll_files
from watcher_common import require_env
from watcher_common import setup_watchdog_for_files


@dataclasses.dataclass(frozen=True)
class _WatcherSettings:
    """Parsed conversation watcher settings from settings.toml."""

    poll_interval: int = 5


def _load_watcher_settings(agent_state_dir: Path) -> _WatcherSettings:
    """Load conversation watcher settings from settings.toml."""
    settings_path = agent_state_dir / "settings.toml"
    try:
        if not settings_path.exists():
            return _WatcherSettings()
        raw = tomllib.loads(settings_path.read_text())
        watchers = raw.get("watchers", {})
        return _WatcherSettings(
            poll_interval=watchers.get("conversation_poll_interval_seconds", 5),
        )
    except Exception as exc:
        print(f"WARNING: failed to load settings: {exc}", file=sys.stderr)
        return _WatcherSettings()


def _get_llm_db_path() -> Path:
    """Locate the llm database file."""
    llm_user_path = os.environ.get("LLM_USER_PATH", "")
    if not llm_user_path:
        llm_user_path = str(Path.home() / ".config" / "io.datasette.llm")
    return Path(llm_user_path) / "logs.db"


def _get_tracked_conversation_ids(conversations_file: Path, log: Logger) -> set[str]:
    """Read tracked conversation IDs from logs/conversations/events.jsonl."""
    tracked_cids: set[str] = set()
    if not conversations_file.is_file():
        return tracked_cids
    try:
        with conversations_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tracked_cids.add(json.loads(line)["conversation_id"])
                except (json.JSONDecodeError, KeyError) as exc:
                    log.info(f"WARNING: malformed conversation event line: {exc}")
                    continue
    except OSError as exc:
        log.info(f"WARNING: failed to read conversations file: {exc}")
    return tracked_cids


def _get_existing_event_ids(messages_file: Path, log: Logger) -> set[str]:
    """Read event IDs already present in logs/messages/events.jsonl."""
    file_event_ids: set[str] = set()
    if not messages_file.is_file():
        return file_event_ids
    try:
        with messages_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    file_event_ids.add(json.loads(line)["event_id"])
                except (json.JSONDecodeError, KeyError) as exc:
                    log.info(f"WARNING: malformed message event line: {exc}")
                    continue
    except OSError as exc:
        log.info(f"WARNING: failed to read messages file: {exc}")
    return file_event_ids


def _sync_messages(
    db_path: Path,
    conversations_file: Path,
    messages_file: Path,
    log: Logger,
) -> int:
    """Sync missing messages from the llm DB to logs/messages/events.jsonl.

    Uses an adaptive window: starts by fetching the most recent 200 responses
    from the DB and checks which event IDs are missing from the output file.
    If ALL fetched events are missing (suggesting the file is far behind),
    doubles the window and retries until it finds events already in the file
    or runs out of DB rows.

    Returns the number of new events synced.
    """
    if not db_path.is_file():
        log.debug(f"LLM database not found at {db_path}")
        return 0

    tracked_cids = _get_tracked_conversation_ids(conversations_file, log)
    if not tracked_cids:
        return 0

    file_event_ids = _get_existing_event_ids(messages_file, log)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.info(f"WARNING: cannot open database: {exc}")
        return 0

    placeholders = ",".join("?" for _ in tracked_cids)
    cid_list = list(tracked_cids)

    window = 200
    missing_events: list[tuple[str, int, str]] = []

    while True:
        try:
            rows = conn.execute(
                f"SELECT id, datetime_utc, conversation_id, prompt, response "
                f"FROM responses "
                f"WHERE conversation_id IN ({placeholders}) "
                f"ORDER BY datetime_utc DESC "
                f"LIMIT ?",
                [*cid_list, window],
            ).fetchall()
        except sqlite3.Error as exc:
            log.info(f"WARNING: sqlite3 query error: {exc}")
            break

        if not rows:
            break

        missing_events = []
        is_found_existing = False

        for row_id, ts, cid, prompt, response in rows:
            if prompt:
                eid = f"{row_id}-user"
                if eid in file_event_ids:
                    is_found_existing = True
                else:
                    missing_events.append(
                        (
                            ts,
                            0,
                            json.dumps(
                                {
                                    "timestamp": ts,
                                    "type": "message",
                                    "event_id": eid,
                                    "source": "messages",
                                    "conversation_id": cid,
                                    "role": "user",
                                    "content": prompt,
                                },
                                separators=(",", ":"),
                            ),
                        )
                    )

            if response:
                eid = f"{row_id}-assistant"
                if eid in file_event_ids:
                    is_found_existing = True
                else:
                    missing_events.append(
                        (
                            ts,
                            1,
                            json.dumps(
                                {
                                    "timestamp": ts,
                                    "type": "message",
                                    "event_id": eid,
                                    "source": "messages",
                                    "conversation_id": cid,
                                    "role": "assistant",
                                    "content": response,
                                },
                                separators=(",", ":"),
                            ),
                        )
                    )

        if is_found_existing or len(rows) < window:
            break

        window *= 2

    conn.close()

    if not missing_events:
        return 0

    missing_events.sort(key=lambda x: (x[0], x[1]))

    messages_file.parent.mkdir(parents=True, exist_ok=True)
    with messages_file.open("a") as f:
        for _, _, event_json in missing_events:
            f.write(event_json + "\n")

    return len(missing_events)


# --- WATCHDOG-DEPENDENT CODE BELOW (not importable without watchdog) ---


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    host_dir = Path(require_env("MNG_HOST_DIR"))

    conversations_file = agent_state_dir / "logs" / "conversations" / "events.jsonl"
    messages_file = agent_state_dir / "logs" / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True, exist_ok=True)

    log = Logger(host_dir / "logs" / "conversation_watcher.log")

    settings = _load_watcher_settings(agent_state_dir)
    db_path = _get_llm_db_path()

    log.info("Conversation watcher started")
    log.info(f"  Agent data dir: {agent_state_dir}")
    log.info(f"  LLM database: {db_path}")
    log.info(f"  Conversations events: {conversations_file}")
    log.info(f"  Messages events: {messages_file}")
    log.info(f"  Log file: {log.log_file_path}")
    log.info(f"  Poll interval: {settings.poll_interval}s")
    log.info("  Using watchdog for file watching with periodic mtime polling")

    watch_paths = [db_path, conversations_file]

    wake_event = threading.Event()
    observer, is_watchdog_active = setup_watchdog_for_files(watch_paths, wake_event, log)

    mtime_cache: dict[str, tuple[float, int]] = {}
    mtime_poll_files(watch_paths, mtime_cache, log)

    try:
        while True:
            is_triggered_by_watchdog = wake_event.wait(timeout=settings.poll_interval)
            wake_event.clear()

            if is_triggered_by_watchdog:
                log.debug("Woken by watchdog filesystem event")

            is_mtime_changed = mtime_poll_files(watch_paths, mtime_cache, log)
            if not is_triggered_by_watchdog and is_mtime_changed:
                log.info("Periodic mtime poll detected changes")

            synced_count = _sync_messages(db_path, conversations_file, messages_file, log)
            if synced_count > 0:
                log.info(f"Synced {synced_count} new message event(s) -> logs/messages/events.jsonl")
            else:
                log.debug("No new messages to sync")
    except KeyboardInterrupt:
        log.info("Conversation watcher stopping (KeyboardInterrupt)")
    finally:
        if is_watchdog_active:
            observer.stop()
            observer.join()


if __name__ == "__main__":
    main()
