#!/usr/bin/env python3
"""Conversation watcher for changeling agents.

Syncs messages from the llm database to the standard event log at
events/messages/events.jsonl. Uses watchdog for fast filesystem event
detection, with periodic mtime-based polling as a safety net.

Usage: python3 conversation_watcher.py

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
  MNG_HOST_DIR         - host data directory (contains events/ for event and log output)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from loguru import logger

try:
    from imbue.mng_claude_changeling.resources.watcher_common import load_watchers_section
    from imbue.mng_claude_changeling.resources.watcher_common import read_event_ids_from_jsonl
    from imbue.mng_claude_changeling.resources.watcher_common import require_env
    from imbue.mng_claude_changeling.resources.watcher_common import run_watcher_loop
    from imbue.mng_claude_changeling.resources.watcher_common import setup_watcher_logging
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from watcher_common import load_watchers_section  # type: ignore[no-redef]
    from watcher_common import read_event_ids_from_jsonl  # type: ignore[no-redef]
    from watcher_common import require_env  # type: ignore[no-redef]
    from watcher_common import run_watcher_loop  # type: ignore[no-redef]
    from watcher_common import setup_watcher_logging  # type: ignore[no-redef]


def _load_poll_interval(agent_work_dir: Path) -> int:
    """Load conversation watcher poll interval from settings.toml."""
    watchers = load_watchers_section(agent_work_dir)
    return watchers.get("conversation_poll_interval_seconds", 5)


def _get_llm_db_path() -> Path:
    """Locate the llm database file."""
    llm_user_path = os.environ.get("LLM_USER_PATH", "")
    if not llm_user_path:
        llm_user_path = str(Path.home() / ".config" / "io.datasette.llm")
    return Path(llm_user_path) / "logs.db"


def _get_tracked_conversation_ids(conversations_file: Path) -> set[str]:
    """Read tracked conversation IDs from events/conversations/events.jsonl."""
    tracked_conversation_ids: set[str] = set()
    if not conversations_file.is_file():
        return tracked_conversation_ids
    try:
        with conversations_file.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tracked_conversation_ids.add(json.loads(line)["conversation_id"])
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.warning("Malformed conversation event line: {}", exc)
                    continue
    except OSError as exc:
        logger.warning("Failed to read conversations file: {}", exc)
    return tracked_conversation_ids


def _sync_messages(
    db_path: Path,
    conversations_file: Path,
    messages_file: Path,
) -> int:
    """Sync missing messages from the llm DB to events/messages/events.jsonl.

    Uses an adaptive window: starts by fetching the most recent 200 responses
    from the DB and checks which event IDs are missing from the output file.
    If ALL fetched events are missing (suggesting the file is far behind),
    doubles the window and retries until it finds events already in the file
    or runs out of DB rows.

    Returns the number of new events synced.
    """
    if not db_path.is_file():
        logger.debug("LLM database not found at {}", db_path)
        return 0

    tracked_conversation_ids = _get_tracked_conversation_ids(conversations_file)
    if not tracked_conversation_ids:
        return 0

    file_event_ids = read_event_ids_from_jsonl(messages_file)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("Cannot open database: {}", exc)
        return 0

    placeholders = ",".join("?" for _ in tracked_conversation_ids)
    conversation_id_list = list(tracked_conversation_ids)

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
                [*conversation_id_list, window],
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("sqlite3 query error: {}", exc)
            break

        if not rows:
            break

        missing_events = []
        is_found_existing = False

        for row_id, ts, conversation_id, prompt, response in rows:
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
                                    "conversation_id": conversation_id,
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
                                    "conversation_id": conversation_id,
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


def main() -> None:
    agent_state_dir = Path(require_env("MNG_AGENT_STATE_DIR"))
    agent_work_dir = Path(require_env("MNG_AGENT_WORK_DIR"))
    host_dir = Path(require_env("MNG_HOST_DIR"))

    conversations_file = agent_state_dir / "events" / "conversations" / "events.jsonl"
    messages_file = agent_state_dir / "events" / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True, exist_ok=True)

    setup_watcher_logging("conversation_watcher", host_dir / "events" / "logs")

    poll_interval = _load_poll_interval(agent_work_dir)
    db_path = _get_llm_db_path()

    logger.info("Conversation watcher started")
    logger.info("  Agent data dir: {}", agent_state_dir)
    logger.info("  LLM database: {}", db_path)
    logger.info("  Conversations events: {}", conversations_file)
    logger.info("  Messages events: {}", messages_file)
    logger.info("  Poll interval: {}s", poll_interval)

    watch_paths = [db_path, conversations_file]

    def on_tick() -> None:
        synced_count = _sync_messages(db_path, conversations_file, messages_file)
        if synced_count > 0:
            logger.info("Synced {} new message event(s) -> events/messages/events.jsonl", synced_count)
        else:
            logger.debug("No new messages to sync")

    run_watcher_loop(
        "Conversation watcher",
        poll_interval,
        watch_paths,
        is_directory_mode=False,
        on_tick=on_tick,
    )


if __name__ == "__main__":
    main()
