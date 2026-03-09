"""Non-fixture test utilities for mng-changeling-chat.

Factory functions, helpers, and concrete test implementations that are
explicitly imported by test files.
"""

import json
import sqlite3

from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.hosts.host import Host
from imbue.mng_changeling_chat.api import get_agent_state_dir


class TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name."""

    def get_expected_process_name(self) -> str:
        return "test-process"


def create_conversation_events(
    host: Host,
    agent: TestAgent,
    conversations: list[dict[str, object]],
) -> None:
    """Create conversation records in the llm database for testing.

    Each conversation dict should have at minimum: conversation_id, model.
    Optional fields: timestamp (used as created_at), tags (dict).
    """
    agent_state_dir = get_agent_state_dir(agent, host)
    db_path = agent_state_dir / "llm_data" / "logs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS changeling_conversations ("
            "conversation_id TEXT PRIMARY KEY, "
            "tags TEXT NOT NULL DEFAULT '{}', "
            "created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, name TEXT, model TEXT)")
        for conv in conversations:
            conversation_id = conv["conversation_id"]
            model = conv.get("model", "?")
            created_at = conv.get("timestamp", "")
            tags = conv.get("tags", {})
            if isinstance(tags, str):
                tags_json = tags
            else:
                tags_json = json.dumps(tags)
            conn.execute(
                "INSERT OR REPLACE INTO changeling_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
                (conversation_id, tags_json, created_at),
            )
            conn.execute(
                "INSERT OR REPLACE INTO conversations (id, name, model) VALUES (?, ?, ?)",
                (conversation_id, conversation_id, model),
            )
        conn.commit()


def create_message_events(
    host: Host,
    agent: TestAgent,
    messages: list[dict[str, str]],
) -> None:
    """Create message event files on the host for testing."""
    agent_state_dir = get_agent_state_dir(agent, host)
    msg_dir = agent_state_dir / "events" / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for msg in messages:
        lines.append(json.dumps(msg))
    (msg_dir / "events.jsonl").write_text("\n".join(lines) + "\n")
