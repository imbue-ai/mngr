"""Tests for the register-conversations webchat plugin."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from llm.cli import logs_db_path
from llm_webchat.database import open_database
from starlette.routing import Route

from imbue.mngr_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mngr_llm.resources.webchat_plugins.webchat_register_conversations import RegisterConversationsPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_register_conversations import (
    _register_conversation_in_mind_table,
)


def _create_app_with_plugin(db_path: Path) -> FastAPI:
    """Create a FastAPI app with the register-conversations plugin and minimal llm-webchat state."""
    app = FastAPI()
    app.state.database = open_database()
    plugin = RegisterConversationsPlugin(db_path=db_path)
    plugin.endpoint(app=app)
    return app


def test_register_conversation_in_mind_table_creates_record(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_mind_conversations_table_in_test_db(db_path)

    _register_conversation_in_mind_table(db_path, "conv-reg-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id, tags FROM mind_conversations WHERE conversation_id = ?",
        ("conv-reg-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "conv-reg-001"
    tags = json.loads(rows[0][1])
    assert tags["name"] == "(new chat)"


def test_register_conversation_in_mind_table_creates_table_if_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    # Create an empty database without the mind_conversations table
    conn = sqlite3.connect(str(db_path))
    conn.close()

    _register_conversation_in_mind_table(db_path, "conv-auto-table-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
        ("conv-auto-table-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_register_conversation_in_mind_table_ignores_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "logs.db"
    create_mind_conversations_table_in_test_db(db_path)

    _register_conversation_in_mind_table(db_path, "conv-dup-001")
    _register_conversation_in_mind_table(db_path, "conv-dup-001")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
        ("conv-dup-001",),
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_register_conversation_in_mind_table_handles_nonexistent_db(tmp_path: Path) -> None:
    db_path = tmp_path / "nonexistent" / "logs.db"
    # Should not raise; logs a warning instead
    _register_conversation_in_mind_table(db_path, "conv-missing-001")


def test_plugin_registers_route() -> None:
    app = FastAPI()
    plugin = RegisterConversationsPlugin(db_path=Path("/tmp/fake.db"))
    plugin.endpoint(app=app)
    post_routes = [
        r.path for r in app.routes if isinstance(r, Route) and r.methods is not None and "POST" in r.methods
    ]
    assert "/api/conversations" in post_routes


def test_create_conversation_endpoint_registers_in_mind_table(tmp_path: Path) -> None:
    """The overridden POST /api/conversations endpoint creates both the llm conversation and the mind_conversations record."""
    # Use the real llm database path so open_database() works
    db_path = logs_db_path()
    app = _create_app_with_plugin(db_path)
    client = TestClient(app)

    response = client.post(
        "/api/conversations",
        json={"name": "test-conv", "model": "test-model"},
    )

    assert response.status_code == 201
    conversation_id = response.json()["id"]
    assert conversation_id

    # Verify it was registered in mind_conversations
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT conversation_id, tags FROM mind_conversations WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    tags = json.loads(rows[0][1])
    assert tags["name"] == "(new chat)"
