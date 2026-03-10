#!/usr/bin/env python3
"""Helper for chat.sh to interact with the changeling_conversations table.

Provides subcommands for CRUD operations on the changeling_conversations
table in the llm sqlite database, using parameterized queries for safety.

The changeling_conversations table stores only metadata not tracked by
the llm tool's native tables (tags, created_at). The model is stored
in the llm tool's ``conversations`` table and queried from there.

The table schema matches CHANGELING_CONVERSATIONS_TABLE_SQL from provisioning.py.
The table is expected to already exist (created during provisioning). If it
does not, the insert subcommand creates it as a safety net.

Usage:
    mng changeling-conversation-db insert <db_path> <conversation_id> <tags_json> <created_at>
    mng changeling-conversation-db lookup-model <db_path> <conversation_id>
    mng changeling-conversation-db count <db_path>
    mng changeling-conversation-db max-rowid <db_path>
    mng changeling-conversation-db poll-new <db_path> <max_rowid>

Environment: None required (all paths passed as arguments).
"""

import sqlite3
import sys

# SYNC: This schema MUST match CHANGELING_CONVERSATIONS_TABLE_SQL in provisioning.py.
# A test (test_conversation_db_schema_matches_provisioning) verifies they stay in sync.
_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS changeling_conversations ("
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
            "INSERT OR REPLACE INTO changeling_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
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
            row = conn.execute("SELECT count(*) FROM changeling_conversations").fetchone()
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
        case "count":
            count(db_path)
        case "max-rowid":
            max_rowid(db_path)
        case "poll-new":
            poll_new(db_path, sys.argv[3])
        case _ as unreachable:
            _warn(f"Unknown subcommand: {unreachable}")
            sys.exit(1)


if __name__ == "__main__":
    main()
