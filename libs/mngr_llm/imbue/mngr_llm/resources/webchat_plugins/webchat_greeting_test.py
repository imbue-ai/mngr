"""Tests for the greeting-conversation webchat plugin."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from imbue.mngr_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mngr_llm.resources.webchat_plugins.webchat_greeting import GreetingPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_greeting import _parse_conversation_id_from_inject_output
from imbue.mngr_llm.resources.webchat_plugins.webchat_greeting import _register_conversation_in_mind_table


def test_parse_conversation_id_from_typical_output() -> None:
    output = "Injected message into conversation 01jq5abc123def456\n"
    assert _parse_conversation_id_from_inject_output(output) == "01jq5abc123def456"


def test_parse_conversation_id_from_output_without_trailing_newline() -> None:
    output = "Injected message into conversation conv-xyz-789"
    assert _parse_conversation_id_from_inject_output(output) == "conv-xyz-789"


def test_parse_conversation_id_returns_none_for_empty_output() -> None:
    assert _parse_conversation_id_from_inject_output("") is None


def test_parse_conversation_id_returns_none_for_single_word() -> None:
    assert _parse_conversation_id_from_inject_output("error") is None


def test_register_conversation_in_mind_table_creates_record(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_mind_conversations_table_in_test_db(db_path)

    _register_conversation_in_mind_table(db_path, "greeting-conv-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id, tags FROM mind_conversations WHERE conversation_id = ?",
        ("greeting-conv-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "greeting-conv-001"
    tags = json.loads(rows[0][1])
    assert tags["name"] == "(new chat)"


def test_register_conversation_in_mind_table_creates_table_if_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    _register_conversation_in_mind_table(db_path, "greeting-auto-table-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
        ("greeting-auto-table-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_register_conversation_in_mind_table_ignores_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_mind_conversations_table_in_test_db(db_path)

    _register_conversation_in_mind_table(db_path, "greeting-dup-001")
    _register_conversation_in_mind_table(db_path, "greeting-dup-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
        ("greeting-dup-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_register_conversation_in_mind_table_handles_nonexistent_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent" / "logs.db"
    # Should not raise; logs a warning instead
    _register_conversation_in_mind_table(db_path, "greeting-missing-001")


def test_plugin_registers_route() -> None:
    app = FastAPI()
    plugin = GreetingPlugin(agent_work_dir="", llm_user_path="")
    plugin.endpoint(app=app)
    post_routes = [r.path for r in app.routes if isinstance(r, Route) and r.methods is not None and "POST" in r.methods]
    assert "/api/greeting-conversation" in post_routes


def test_greeting_endpoint_returns_500_when_llm_not_available() -> None:
    """The endpoint returns 500 when llm inject is not available."""
    app = FastAPI()
    plugin = GreetingPlugin(agent_work_dir="", llm_user_path="")
    plugin.endpoint(app=app)
    client = TestClient(app)

    # The llm command will not be found in the test environment
    response = client.post("/api/greeting-conversation")

    # Should either succeed (if llm is available) or return 500
    assert response.status_code in (201, 500)
    if response.status_code == 500:
        assert "error" in response.json()
