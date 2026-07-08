"""Test utilities for mngr-opencode (explicitly imported; no fixtures here).

Builders for minimal but real OpenCode-shaped SQLite dbs that the adopt resolver, the rebind
SQL, and the multi-session merge SQL are exercised against. The schema mirrors the columns those
code paths touch on the real opencode 1.17.7 db (``session``/``project``/``project_directory`` plus
the session-scoped ``message``/``part``/``todo``/``session_share`` and project-scoped ``permission``
tables), kept deliberately small so a test only declares the rows it cares about.
"""

import sqlite3
from pathlib import Path

# Mirrors the columns the opencode adopt/rebind/merge paths touch on the real opencode 1.17.7
# schema. ``project_directory`` is included (the rebind upserts into it); session-scoped tables
# carry a ``data`` blob so a merge that copies message/part content is exercised end to end.
_OPENCODE_TEST_SCHEMA: str = (
    "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, directory TEXT NOT NULL);"
    "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);"
    "CREATE TABLE project_directory (project_id TEXT NOT NULL, directory TEXT NOT NULL, time_created INTEGER, "
    "PRIMARY KEY (project_id, directory));"
    "CREATE TABLE permission (project_id TEXT PRIMARY KEY, data TEXT NOT NULL);"
    "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL);"
    "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL, data TEXT NOT NULL);"
    "CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, position INTEGER NOT NULL, "
    "PRIMARY KEY (session_id, position));"
    "CREATE TABLE session_share (session_id TEXT PRIMARY KEY, id TEXT NOT NULL, url TEXT NOT NULL);"
    # Global table (schema bookkeeping) the merge must never copy; present so a test can assert that.
    "CREATE TABLE migration (id TEXT PRIMARY KEY, time_completed INTEGER NOT NULL);"
)


def create_opencode_db(db_path: Path) -> None:
    """Create an empty OpenCode-shaped db (just the schema, no rows)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_OPENCODE_TEST_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def write_opencode_session(
    db_path: Path,
    session_id: str,
    directory: str,
    *,
    parent_id: str | None = None,
    message_id: str | None = None,
) -> str:
    """Add one session (its project, project_directory, and optionally a message) to ``db_path``.

    Creates the db + schema on first use. Returns the owning project id (``proj_<session_id>``).
    When ``message_id`` is given, a message + a part referencing it are filed under the session so
    a merge that must carry conversation content has something to copy. A child session (non-null
    ``parent_id``) reuses the parent's project to mirror how opencode files subagent sessions.
    """
    if not db_path.exists():
        create_opencode_db(db_path)
    project_id = f"proj_{parent_id}" if parent_id is not None else f"proj_{session_id}"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("INSERT OR IGNORE INTO project (id, worktree) VALUES (?, ?)", (project_id, directory))
        connection.execute(
            "INSERT OR IGNORE INTO project_directory (project_id, directory, time_created) VALUES (?, ?, 0)",
            (project_id, directory),
        )
        connection.execute(
            "INSERT INTO session (id, project_id, parent_id, directory) VALUES (?, ?, ?, ?)",
            (session_id, project_id, parent_id, directory),
        )
        if message_id is not None:
            connection.execute(
                "INSERT INTO message (id, session_id, data) VALUES (?, ?, ?)",
                (message_id, session_id, '{"role":"user"}'),
            )
            connection.execute(
                "INSERT INTO part (id, message_id, session_id, data) VALUES (?, ?, ?, ?)",
                (f"prt_{message_id}", message_id, session_id, '{"type":"text"}'),
            )
        connection.commit()
    finally:
        connection.close()
    return project_id
