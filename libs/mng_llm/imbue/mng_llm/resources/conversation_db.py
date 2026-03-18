#!/usr/bin/env python3
"""Helper for chat.sh to interact with the mind_conversations table.

Provides subcommands for CRUD operations on the mind_conversations
table in the llm sqlite database, using parameterized queries for safety.

The mind_conversations table stores only metadata not tracked by
the llm tool's native tables (tags, created_at). The model is stored
in the llm tool's ``conversations`` table and queried from there.

The table schema matches MIND_CONVERSATIONS_TABLE_SQL from provisioning.py.
The table is expected to already exist (created during provisioning). If it
does not, the insert subcommand creates it as a safety net.

Usage:
    mng mind-conversation-db insert <db_path> <conversation_id> <tags_json> <created_at>
    mng mind-conversation-db lookup-model <db_path> <conversation_id>
    mng mind-conversation-db lookup-by-name <db_path> <name>
    mng mind-conversation-db count <db_path>
    mng mind-conversation-db max-rowid <db_path>
    mng mind-conversation-db poll-new <db_path> <max_rowid>
    mng mind-conversation-db last-response-id <db_path> <conversation_id>

Environment: None required (all paths passed as arguments).
"""

import sqlite3
import sys

# SYNC: This schema MUST match MIND_CONVERSATIONS_TABLE_SQL in provisioning.py.
# A test (test_conversation_db_schema_matches_provisioning) verifies they stay in sync.
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS mind_conversations ("
    "conversation_id TEXT PRIMARY KEY, "
    "tags TEXT NOT NULL DEFAULT '{}', "
    "created_at TEXT NOT NULL)"
)


def _write_stdout(value: object) -> None:
    sys.stdout.write(f"{value}\n")
    sys.stdout.flush()


def _warn(message: str) -> None:
    sys.stderr.write(f"WARNING: {message}\n")
    sys.stderr.flush()


def insert(db_path: str, conversation_id: str, tags: str, created_at: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO mind_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
            (conversation_id, tags, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def lookup_model(db_path: str, conversation_id: str) -> None:
    """Look up the model from the llm tool's native conversations table."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT model FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if row:
                _write_stdout(row[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"lookup-model failed: {e}")


def count(db_path: str) -> None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT count(*) FROM mind_conversations").fetchone()
            _write_stdout(row[0] if row else 0)
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"count failed: {e}")
        _write_stdout(0)


def max_rowid(db_path: str) -> None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM conversations").fetchone()
            _write_stdout(row[0] if row else 0)
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"max-rowid failed: {e}")
        _write_stdout(0)


def lookup_by_name(db_path: str, name: str) -> None:
    """Look up a conversation ID by its name tag.

    Searches the mind_conversations table for a conversation whose tags
    JSON contains a ``name`` key matching the given value. Returns the
    most recently created match (by created_at descending).
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT conversation_id FROM mind_conversations "
                "WHERE json_extract(tags, '$.name') = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row:
                _write_stdout(row[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"lookup-by-name failed: {e}")


def last_response_id(db_path: str, conversation_id: str) -> None:
    """Look up the most recently inserted response ID for a conversation.

    Queries the llm tool's ``responses`` table for the response with the
    latest ``datetime_utc`` belonging to the given conversation.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT id FROM responses WHERE conversation_id = ? ORDER BY datetime_utc DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row:
                _write_stdout(row[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"last-response-id failed: {e}")


def poll_new(db_path: str, max_rowid: str) -> None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT id FROM conversations WHERE rowid > ? ORDER BY rowid ASC LIMIT 1",
                (int(max_rowid),),
            ).fetchone()
            if row:
                _write_stdout(row[0])
        finally:
            conn.close()
    except sqlite3.Error as e:
        _warn(f"poll-new failed: {e}")


def main() -> None:
    if len(sys.argv) < 3:
        _warn(f"Usage: {sys.argv[0]} <subcommand> <db_path> [args...]")
        sys.exit(1)

    subcommand = sys.argv[1]
    db_path = sys.argv[2]

    match subcommand:
        case "insert":
            insert(db_path, sys.argv[3], sys.argv[4], sys.argv[5])
        case "lookup-model":
            lookup_model(db_path, sys.argv[3])
        case "lookup-by-name":
            lookup_by_name(db_path, sys.argv[3])
        case "count":
            count(db_path)
        case "max-rowid":
            max_rowid(db_path)
        case "poll-new":
            poll_new(db_path, sys.argv[3])
        case "last-response-id":
            last_response_id(db_path, sys.argv[3])
        case _ as unreachable:
            _warn(f"Unknown subcommand: {unreachable}")
            sys.exit(1)


if __name__ == "__main__":
    main()
