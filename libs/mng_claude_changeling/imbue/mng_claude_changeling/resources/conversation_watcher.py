#!/usr/bin/env python3
"""Conversation watcher for changeling agents.

Syncs messages from the llm database to the standard event log at
events/messages/events.jsonl. Uses watchdog for fast filesystem event
detection, with periodic mtime-based polling as a safety net.

Tracked conversations are read from the changeling_conversations table
in the llm database (created during provisioning).

Usage: mng changeling-conversation-watcher

Environment:
  MNG_AGENT_STATE_DIR  - agent state directory (contains events/)
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from loguru import logger

from imbue.mng_claude_changeling.resources.watcher_common import load_watchers_section
from imbue.mng_claude_changeling.resources.watcher_common import read_event_ids_from_jsonl
from imbue.mng_claude_changeling.resources.watcher_common import require_env
from imbue.mng_claude_changeling.resources.watcher_common import run_watcher_loop
from imbue.mng_claude_changeling.resources.watcher_common import setup_watcher_logging


def _load_poll_interval(agent_work_dir: Path) -> int:
    """Load conversation watcher poll interval from settings.toml."""
    watchers = load_watchers_section(agent_work_dir)
    return watchers.get("conversation_poll_interval_seconds", 5)


def _get_llm_db_path() -> Path:
    """Locate the llm database file.

    Requires LLM_USER_PATH to be set (always configured during provisioning).
    """
    llm_user_path = os.environ.get("LLM_USER_PATH", "")
    if not llm_user_path:
        raise RuntimeError("LLM_USER_PATH must be set")
    return Path(llm_user_path) / "logs.db"


def _get_tracked_conversation_ids(db_path: Path) -> set[str]:
    """Read tracked conversation IDs from the changeling_conversations table in the llm database."""
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("Cannot open database for tracked conversations: {}", exc)
        return set()

    try:
        rows = conn.execute("SELECT conversation_id FROM changeling_conversations").fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error as exc:
        logger.debug("changeling_conversations table not available: {}", exc)
        return set()
    finally:
        conn.close()


def _sync_messages(
    db_path: Path,
    messages_file: Path,
) -> int:
    """Sync missing messages from the llm DB to events/messages/events.jsonl.

    Uses an adaptive window: starts by fetching the most recent 200 responses
    from the DB and checks which event IDs are missing from the output file.
    If ALL fetched events are missing (suggesting the file is far behind),
    doubles the window and retries until it finds events already in the file
    or runs out of DB rows.

    THIS FUNCTION MUST NOT LOG ANYTHING except warnings about database access issues, otherwise the logs get huge and spammy.

    Returns the number of new events synced.
    """
    if not db_path.is_file():
        logger.debug("LLM database not found at {}", db_path)
        return 0

    tracked_conversation_ids = _get_tracked_conversation_ids(db_path)
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
            # llm live-chat inserts a preliminary row (prompt set, response
            # is empty string "") for crash safety before streaming. It is
            # deleted once the real response is logged. Skip these to avoid
            # syncing duplicate user messages.
            if prompt and response == "":
                continue

            # this happens when llm inject creates a message
            # because this is created by the thinking agent itself, there's no need for these messages to be emitted.
            if prompt == "...":
                continue

            # Sync user messages, but skip empty prompts (e.g. from llm inject
            # where the agent injected its own message with prompt="" or "...")
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

            # Skip assistant responses that are only whitespace. These are
            # intermediate tool-call responses from llm live-chat's
            # conversation.chain() -- the actual content comes in the
            # final response of the chain.
            if response and response.strip():
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

    messages_file = agent_state_dir / "events" / "messages" / "events.jsonl"
    messages_file.parent.mkdir(parents=True, exist_ok=True)

    setup_watcher_logging("conversation_watcher", agent_state_dir / "events" / "logs")

    poll_interval = _load_poll_interval(agent_work_dir)
    db_path = _get_llm_db_path()

    logger.info("Conversation watcher started")
    logger.info("  Agent data dir: {}", agent_state_dir)
    logger.info("  LLM database: {}", db_path)
    logger.info("  Messages events: {}", messages_file)
    logger.info("  Poll interval: {}s", poll_interval)

    watch_paths = [db_path]

    def on_tick() -> None:
        synced_count = _sync_messages(db_path, messages_file)
        if synced_count > 0:
            logger.info("Synced {} new message event(s) -> events/messages/events.jsonl", synced_count)
        else:
            # IT IS IMPERATIVE THAT WE NOT LOG inside of this part of the code--otherwise the logs get huge and spammy,
            # logger.debug("No new messages to sync")
            pass

    run_watcher_loop(
        "Conversation watcher",
        poll_interval,
        watch_paths,
        is_directory_mode=False,
        on_tick=on_tick,
    )


if __name__ == "__main__":
    main()
