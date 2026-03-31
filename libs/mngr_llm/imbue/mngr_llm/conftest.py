import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from loguru import logger

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()


LLM_RESPONSES_SCHEMA = """
    CREATE TABLE responses (
        id TEXT PRIMARY KEY,
        system TEXT,
        prompt TEXT,
        response TEXT,
        model TEXT,
        datetime_utc TEXT,
        conversation_id TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        token_details TEXT,
        response_json TEXT,
        reply_to_id TEXT,
        chat_id INTEGER,
        duration_ms INTEGER,
        attachment_type TEXT,
        attachment_path TEXT,
        attachment_url TEXT,
        attachment_content TEXT
    )
"""

LLM_CONVERSATIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        name TEXT,
        model TEXT
    )
"""


def write_minds_settings_toml(base_dir: Path, content: str) -> Path:
    """Write minds.toml in the given directory for watcher tests."""
    settings_path = base_dir / "minds.toml"
    settings_path.write_text(content)
    return settings_path


def create_mind_conversations_table_in_test_db(db_path: Path) -> None:
    """Create the mind_conversations and llm conversations tables in the given database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        conn.execute(LLM_CONVERSATIONS_SCHEMA)
        conn.commit()


def create_test_llm_db(db_path: Path, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    """Create a minimal llm-compatible SQLite database with responses and mind_conversations.

    Each row is (id, prompt, response, model, datetime_utc, conversation_id).
    """
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(LLM_RESPONSES_SCHEMA)
        conn.execute(LLM_CONVERSATIONS_SCHEMA)
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        for row_id, prompt, response, model, dt, conversation_id in rows:
            conn.execute(
                "INSERT INTO responses (id, prompt, response, model, datetime_utc, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, prompt, response, model, dt, conversation_id),
            )
        conn.commit()


def write_conversation_to_db(
    db_path: Path,
    conversation_id: str,
    model: str = "claude-sonnet-4-6",
    tags: str = "{}",
    created_at: str = "2025-01-15T10:00:00.000Z",
) -> None:
    """Insert a conversation into both the mind and llm conversations tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        conn.execute(LLM_CONVERSATIONS_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO mind_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
            (conversation_id, tags, created_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO conversations (id, name, model) VALUES (?, ?, ?)",
            (conversation_id, conversation_id, model),
        )
        conn.commit()
